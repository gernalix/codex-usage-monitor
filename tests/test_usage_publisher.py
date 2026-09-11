from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import deploy_runtime
import codex_usage_publisher as publisher
import codex_usage_publisher_legacy as legacy


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def session_rows(session_id: str, prompt_id: str = "123456", second_prompt: str | None = None, prompt_label: str = "PROMPT_ID:") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"timestamp": "2026-09-05T10:00:00Z", "type": "session_meta", "payload": {"session_id": session_id, "cwd": "/tmp"}},
        {"timestamp": "2026-09-05T10:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-test", "collaboration_mode": {"settings": {"reasoning_effort": "low"}}}},
        {"timestamp": "2026-09-05T10:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": f"{prompt_label} {prompt_id} do it"}]}},
        {"timestamp": "2026-09-05T10:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
        {"timestamp": "2026-09-05T10:00:04Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 40, "reasoning_output_tokens": 5, "total_tokens": 140}}, "rate_limits": {"primary": {"used_percent": 35, "resets_at": 1788765593}}}},
        {"timestamp": "2026-09-05T10:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"text": "PASS\nok"}], "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"}}},
        {"timestamp": "2026-09-05T10:00:06Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "PASS\nok", "started_at": 1788602401, "completed_at": 1788602406, "duration_ms": 5000}},
    ]
    if second_prompt:
        rows.extend(
            [
                {"timestamp": "2026-09-05T10:01:00Z", "type": "turn_context", "payload": {"turn_id": "turn-2", "model": "gpt-test", "collaboration_mode": {"settings": {"reasoning_effort": "medium"}}}},
                {"timestamp": "2026-09-05T10:01:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": f"PROMPT_ID={second_prompt} continue"}]}},
                {"timestamp": "2026-09-05T10:01:02Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "shell"}},
                {"timestamp": "2026-09-05T10:01:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 20, "reasoning_output_tokens": 2, "total_tokens": 70}}}},
                {"timestamp": "2026-09-05T10:01:04Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-2", "last_agent_message": "DONE text", "duration_ms": 3000}},
            ]
        )
    return rows


def published_cycle_sql(cycle: dict[str, object]) -> tuple[object, ...]:
    metrics = cycle["metrics"]
    return (
        metrics["cycle_key"],
        metrics["native_session_id"],
        metrics["chat_id"],
        cycle["final_event_id"],
        metrics["source_path"],
        cycle["source_sha256"],
        "abc123",
        publisher.legacy.utc_stamp(),
    )


class UsagePublisherTests(unittest.TestCase):
    def make_git_pair(self, root: Path) -> tuple[Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        bare = root / "origin.git"
        repo = root / "repo"
        publisher.run(["git", "init", "--bare", str(bare)])
        publisher.run(["git", "clone", str(bare), str(repo)])
        publisher.run(["git", "config", "user.email", "test@example.invalid"], repo)
        publisher.run(["git", "config", "user.name", "Test"], repo)
        (repo / "file.txt").write_text("base\n", encoding="utf-8")
        publisher.run(["git", "add", "file.txt"], repo)
        publisher.run(["git", "commit", "-m", "base"], repo)
        publisher.run(["git", "branch", "-M", "main"], repo)
        publisher.run(["git", "push", "-u", "origin", "main"], repo)
        return repo, bare

    def test_prompt_id_parser_accepts_markdown_escaped_label(self) -> None:
        self.assertEqual(publisher.prompt_id_from_text("PROMPT_ID=123456"), "123456")
        self.assertEqual(publisher.prompt_id_from_text("PROMPT\\_ID=123456"), "123456")
        self.assertEqual(publisher.prompt_id_from_text("PROMPT_ID: 123456"), "123456")
        self.assertEqual(publisher.prompt_id_from_text("PROMPT\\_ID: 123456"), "123456")
        self.assertIsNone(publisher.prompt_id_from_text("no prompt here"))
        self.assertIsNone(publisher.prompt_id_from_text("XPROMPT_ID=123456"))

    def test_prompt_id_chat_id_multiple_finals_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "sessions/rollout-2026-09-05T10-00-00-019fd1da-cc5d-7db1-b880-a14be6111c38.jsonl"
            write_jsonl(session, session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38", second_prompt="234567"))
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
                again, _ = publisher.parse_session(session, con)
                chat_rows = con.execute("SELECT chat_id, session_id FROM session_chats").fetchall()
            self.assertEqual([c["metrics"]["prompt_id"] for c in cycles], ["123456", "234567"])
            self.assertEqual(len({c["metrics"]["chat_id"] for c in cycles}), 1)
            self.assertEqual(cycles[0]["metrics"]["tool_call_count"], 1)
            self.assertEqual(cycles[0]["metrics"]["uncached_input_tokens"], 75)
            self.assertEqual(cycles[0]["metrics"]["cache_ratio"], 0.25)
            self.assertEqual(cycles[1]["metrics"]["status"], "UNKNOWN")
            self.assertEqual(len(chat_rows), 1)
            self.assertEqual(again[0]["metrics"]["chat_id"], cycles[0]["metrics"]["chat_id"])

    def test_export_backfill_is_idempotent_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            session = root / "s.jsonl"
            rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38")
            rows[2]["payload"]["content"][0]["text"] += " token 123456:" + "abcdefghijklmnopqrstuvwxyz"
            write_jsonl(session, rows)
            with publisher.connect_state(root / "state") as con:
                cycles, events = publisher.parse_session(session, con)
            publisher.export_repo(repo, cycles, {1: events}, root / "missing.sqlite")
            first = {p.relative_to(repo): p.read_text(encoding="utf-8") for p in repo.rglob("*") if p.is_file()}
            publisher.export_repo(repo, cycles, {1: events}, root / "missing.sqlite")
            second = {p.relative_to(repo): p.read_text(encoding="utf-8") for p in repo.rglob("*") if p.is_file()}
            self.assertEqual(first, second)
            text = (repo / "prompts/123456/transcript.jsonl").read_text(encoding="utf-8")
            self.assertIn("[TELEGRAM_TOKEN_REDACTED]", text)

    def test_unassigned_cycle_recovers_prompt_without_renumbering_or_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            session = root / "s.jsonl"
            write_jsonl(session, session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38", "583216", prompt_label="PROMPT\\_ID="))
            with publisher.connect_state(root / "state") as con:
                cycles, events = publisher.parse_session(session, con)
            metrics = cycles[0]["metrics"]
            stale = repo / "prompts/unassigned" / metrics["cycle_key"]
            stale.mkdir(parents=True)
            (stale / "metrics.json").write_text('{"prompt_id":null}\n', encoding="utf-8")

            publisher.export_repo(repo, cycles, {metrics["chat_id"]: events}, root / "missing.sqlite")
            publisher.export_repo(repo, cycles, {metrics["chat_id"]: events}, root / "missing.sqlite")

            self.assertEqual(metrics["prompt_id"], "583216")
            self.assertEqual(metrics["chat_id"], 1)
            self.assertEqual(metrics["cycle_key"], "834af07bf1e8f14fdaacc244")
            self.assertTrue((repo / "prompts/583216/metrics.json").is_file())
            self.assertFalse(stale.exists())
            prompts = (repo / "index/prompts.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(prompts), 1)
            self.assertIn('"prompt_id": "583216"', prompts[0])
            chats = (repo / "index/chats.jsonl").read_text(encoding="utf-8")
            self.assertIn('"chat_id": 1', chats)

    def test_push_failure_does_not_send_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sessions"
            write_jsonl(source / "s.jsonl", session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38"))
            argv = ["--source-root", str(source), "--state-dir", str(root / "state"), "--data-repo", str(root / "repo"), "run"]
            with mock.patch.object(publisher, "assert_private_repo"), mock.patch.object(publisher, "ensure_repo") as ensure, mock.patch.object(publisher, "git_ok", side_effect=publisher.PublisherError("push failed")), mock.patch.object(publisher, "send_batch_telegram") as send:
                ensure.side_effect = lambda repo, _remote: repo.mkdir(parents=True, exist_ok=True) or publisher.run(["git", "init", "-b", "main"], repo)
                self.assertEqual(publisher.main(argv), 75)
                send.assert_not_called()

    def test_successful_push_sends_telegram_once_and_noop_stays_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sessions"
            repo = root / "repo"
            write_jsonl(source / "s.jsonl", session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38"))
            publisher.run(["git", "init", "-b", "main", str(repo)])
            publisher.run(["git", "config", "user.email", "test@example.invalid"], repo)
            publisher.run(["git", "config", "user.name", "Test"], repo)
            argv = ["--source-root", str(source), "--state-dir", str(root / "state"), "--data-repo", str(repo), "run"]
            with mock.patch.object(publisher, "assert_private_repo"), mock.patch.object(publisher, "ensure_repo"), mock.patch.object(publisher, "git_ok") as git_ok, mock.patch.object(publisher, "send_batch_telegram", return_value=True) as send:
                git_ok.side_effect = lambda cmd, cwd, timeout=120: publisher.run(cmd, cwd)
                self.assertEqual(publisher.main(argv), 0)
                self.assertEqual(publisher.main(argv), 0)
            self.assertEqual(send.call_count, 1)

    def test_publisher_telegram_skips_normal_pushes_and_compacts_missing_prompt_id(self) -> None:
        normal_cycle = {"metrics": {"chat_id": 178, "prompt_id": "417826", "cycle_key": "normal"}}
        missing_cycles = [
            {"metrics": {"chat_id": 178, "prompt_id": None, "cycle_key": f"missing-{index}"}}
            for index in range(10)
        ]
        with mock.patch.object(legacy.quota, "send_telegram") as send:
            self.assertTrue(publisher._legacy_send_batch_telegram([normal_cycle], False))
            send.assert_not_called()
            self.assertTrue(publisher._legacy_send_batch_telegram(missing_cycles, False))
        send.assert_called_once()
        title, message = send.call_args.args[1:]
        self.assertEqual(title, "Codex usage")
        self.assertIn("10 cicli final-response senza PROMPT_ID", message)
        self.assertIn("missing-0", message)
        self.assertIn("missing-9", message)

    def test_result_status_mapping_accepts_explicit_terminal_result(self) -> None:
        self.assertEqual("PASS", publisher.status_from_final("PROMPT_ID=1\nRESULT=PASS"))
        self.assertEqual("PASS", publisher.status_from_final("RESULT: `PASS`"))
        self.assertEqual("BLOCKED", publisher.status_from_final("RESULT=BLOCKED"))
        self.assertEqual("FAIL", publisher.status_from_final("RESULT: FAIL"))
        self.assertEqual("UNKNOWN", publisher.status_from_final("No explicit result here."))

    def test_missing_explicit_path_does_not_crash_repo_resolution(self) -> None:
        self.assertIsNone(publisher._repo_root("/tmp/path-that-does-not-exist-for-codex-guard/file.txt"))

    def test_628431_equivalent_result_pass_is_idempotent_without_extra_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sessions"
            repo = root / "repo"
            rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38", prompt_id="628431")
            rows[-2]["payload"]["content"][0]["text"] = "PROMPT_ID=628431\nRESULT=PASS"
            rows[-1]["payload"]["last_agent_message"] = "PROMPT_ID=628431\nRESULT=PASS"
            write_jsonl(source / "s.jsonl", rows)
            publisher.run(["git", "init", "-b", "main", str(repo)])
            publisher.run(["git", "config", "user.email", "test@example.invalid"], repo)
            publisher.run(["git", "config", "user.name", "Test"], repo)
            argv = ["--source-root", str(source), "--state-dir", str(root / "state"), "--data-repo", str(repo), "run"]
            with mock.patch.object(publisher, "assert_private_repo"), mock.patch.object(publisher, "ensure_repo"), mock.patch.object(publisher, "git_ok") as git_ok, mock.patch.object(publisher, "send_batch_telegram", return_value=True) as send:
                git_ok.side_effect = lambda cmd, cwd, timeout=120: publisher.run(cmd, cwd)
                self.assertEqual(publisher.main(argv), 0)
                self.assertEqual(publisher.main(argv), 0)
            metrics = json.loads((repo / "prompts/628431/metrics.json").read_text(encoding="utf-8"))
            self.assertEqual("PASS", metrics["status"])
            self.assertEqual(send.call_count, 1)

    def test_git_completion_guard_classifies_clean_dirty_ahead_diverged_and_no_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, bare = self.make_git_pair(root)
            self.assertEqual("clean_synced", publisher.classify_git_repo(repo)["status"])

            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            self.assertEqual("dirty", publisher.classify_git_repo(repo)["status"])
            (repo / "dirty.txt").unlink()

            (repo / "file.txt").write_text("ahead\n", encoding="utf-8")
            publisher.run(["git", "commit", "-am", "ahead"], repo)
            self.assertEqual("ahead", publisher.classify_git_repo(repo)["status"])

            other = root / "other"
            publisher.run(["git", "clone", str(bare), str(other)])
            publisher.run(["git", "config", "user.email", "test@example.invalid"], other)
            publisher.run(["git", "config", "user.name", "Test"], other)
            publisher.run(["git", "checkout", "main"], other)
            (other / "remote.txt").write_text("remote\n", encoding="utf-8")
            publisher.run(["git", "add", "remote.txt"], other)
            publisher.run(["git", "commit", "-m", "remote"], other)
            publisher.run(["git", "push", "origin", "main"], other)
            self.assertEqual("diverged", publisher.classify_git_repo(repo)["status"])

            local = root / "local"
            publisher.run(["git", "init", "-b", "main", str(local)])
            publisher.run(["git", "config", "user.email", "test@example.invalid"], local)
            publisher.run(["git", "config", "user.name", "Test"], local)
            (local / "file.txt").write_text("local\n", encoding="utf-8")
            publisher.run(["git", "add", "file.txt"], local)
            publisher.run(["git", "commit", "-m", "local"], local)
            self.assertEqual("no_upstream", publisher.classify_git_repo(local)["status"])

    def test_git_completion_guard_fetch_failure_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _bare = self.make_git_pair(Path(tmp))
            calls = []

            def fake_run(args, cwd, timeout=30):
                calls.append(args)
                if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "main\n", "")
                if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "origin/main\n", "")
                if args[:2] == ["fetch", "origin"]:
                    return publisher.subprocess.CompletedProcess(args, 1, "", "network down")
                return publisher.subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(publisher, "_run_git", side_effect=fake_run):
                result = publisher.classify_git_repo(repo)
            self.assertEqual("unknown", result["status"])
            self.assertEqual("fetch_failed", result["detail"])

    def test_git_completion_guard_status_and_upstream_errors_are_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _bare = self.make_git_pair(Path(tmp))

            def fake_status_failure(args, cwd, timeout=30):
                if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "main\n", "")
                if args[:4] == ["rev-parse", "--verify", "--quiet", "@{u}"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "", "")
                if args[:4] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "origin/main\n", "")
                if args[:2] == ["fetch", "origin"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["status", "--porcelain"]:
                    return publisher.subprocess.CompletedProcess(args, 1, "", "status failed")
                return publisher.subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(publisher, "_run_git", side_effect=fake_status_failure):
                result = publisher.classify_git_repo(repo)
            self.assertEqual("unknown", result["status"])
            self.assertEqual("status_failed", result["detail"])

            def fake_upstream_error(args, cwd, timeout=30):
                if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "main\n", "")
                if args[:4] == ["rev-parse", "--verify", "--quiet", "@{u}"]:
                    return publisher.subprocess.CompletedProcess(args, 2, "", "ambiguous")
                return publisher.subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(publisher, "_run_git", side_effect=fake_upstream_error):
                result = publisher.classify_git_repo(repo)
            self.assertEqual("unknown", result["status"])
            self.assertEqual("upstream_check_failed", result["detail"])

    def test_git_completion_guard_rev_list_failure_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _bare = self.make_git_pair(Path(tmp))

            def fake_run(args, cwd, timeout=30):
                if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "main\n", "")
                if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "origin/main\n", "")
                if args[:2] == ["fetch", "origin"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["status", "--porcelain"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "", "")
                if args[:3] == ["rev-list", "--left-right", "--count"]:
                    return publisher.subprocess.CompletedProcess(args, 0, "not counts\n", "")
                return publisher.subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(publisher, "_run_git", side_effect=fake_run):
                result = publisher.classify_git_repo(repo)
            self.assertEqual("unknown", result["status"])
            self.assertEqual("rev_list_malformed", result["detail"])

    def test_git_completion_guard_records_once_per_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _bare = self.make_git_pair(root)
            session = root / "sessions" / "s.jsonl"
            rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38")
            rows[0]["payload"]["cwd"] = str(repo)
            write_jsonl(session, rows)
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
                cycle = cycles[0]
                metrics = cycle["metrics"]
                con.execute(
                    """
                    INSERT INTO cycles (cycle_key,session_id,chat_id,final_event_id,source_path,source_sha256,published_commit,updated_at_utc)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        metrics["cycle_key"],
                        metrics["native_session_id"],
                        metrics["chat_id"],
                        cycle["final_event_id"],
                        metrics["source_path"],
                        cycle["source_sha256"],
                        "abc123",
                        publisher.legacy.utc_stamp(),
                    ),
                )
                first = publisher.record_git_completion_guard(con, cycle)
                second = publisher.record_git_completion_guard(con, cycle)
                rows_count = con.execute("SELECT COUNT(*) FROM git_completion_guard_repos").fetchone()[0]
            self.assertEqual(["clean_synced"], [row["status"] for row in first])
            self.assertFalse(first[0]["idempotent"])
            self.assertTrue(second[0]["idempotent"])
            self.assertEqual(1, rows_count)

    def test_git_completion_guard_records_three_tool_repos_with_cwd_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repos = [self.make_git_pair(root / f"pair{i}")[0] for i in range(3)]
            session = root / "sessions" / "s.jsonl"
            rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38")
            rows[0]["payload"]["cwd"] = str(root / "not-a-repo")
            rows[1]["payload"]["cwd"] = str(root / "also-not-a-repo")
            tool_rows = []
            for idx, repo in enumerate(repos):
                tool_rows.append(
                    {
                        "timestamp": f"2026-09-05T10:00:0{idx + 3}Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "true", "workdir": str(repo)}),
                        },
                    }
                )
            rows = rows[:3] + tool_rows + rows[4:]
            write_jsonl(session, rows)
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
                cycle = cycles[0]
                con.execute(
                    """
                    INSERT INTO cycles (cycle_key,session_id,chat_id,final_event_id,source_path,source_sha256,published_commit,updated_at_utc)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    published_cycle_sql(cycle),
                )
                recorded = publisher.record_git_completion_guard(con, cycle)
                again = publisher.record_git_completion_guard(con, cycle)
                repo_rows = con.execute("SELECT repo_path,status FROM git_completion_guard_repos ORDER BY repo_path").fetchall()
            self.assertEqual(3, len(recorded))
            self.assertEqual(3, len(repo_rows))
            self.assertEqual([str(repo) for repo in repos], [row["repo_path"] for row in repo_rows])
            self.assertEqual(["clean_synced", "clean_synced", "clean_synced"], [row["status"] for row in repo_rows])
            self.assertTrue(all(row["idempotent"] for row in again))

    def test_explicit_tool_repos_exclude_session_cwd_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = self.make_git_pair(root / "pair-a")[0]
            explicit_repos = [self.make_git_pair(root / f"pair-{name}")[0] for name in ("b", "c", "d")]
            session = root / "sessions" / "s.jsonl"
            rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38")
            rows[0]["payload"]["cwd"] = str(repo_a)
            tool_rows = []
            for idx, repo in enumerate(explicit_repos):
                tool_rows.append(
                    {
                        "timestamp": f"2026-09-05T10:00:0{idx + 3}Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "true", "workdir": str(repo)}),
                        },
                    }
                )
            rows = rows[:3] + tool_rows + rows[4:]
            write_jsonl(session, rows)
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
                cycle = cycles[0]
                con.execute(
                    """
                    INSERT INTO cycles (cycle_key,session_id,chat_id,final_event_id,source_path,source_sha256,published_commit,updated_at_utc)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    published_cycle_sql(cycle),
                )
                recorded = publisher.record_git_completion_guard(con, cycle)
                again = publisher.record_git_completion_guard(con, cycle)
            self.assertEqual([str(repo) for repo in explicit_repos], cycle["metrics"]["repo_projects"])
            self.assertNotIn(str(repo_a), cycle["metrics"]["repo_projects"])
            self.assertEqual([str(repo) for repo in explicit_repos], [row["repo_path"] for row in recorded])
            self.assertTrue(all(row["idempotent"] for row in again))

    def test_session_cwd_repo_is_fallback_when_no_explicit_repo_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _bare = self.make_git_pair(root / "pair")
            session = root / "sessions" / "s.jsonl"
            rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38")
            rows[0]["payload"]["cwd"] = str(repo)
            write_jsonl(session, rows)
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
            self.assertEqual([str(repo)], cycles[0]["metrics"]["repo_projects"])

    def test_parse_session_reads_rollout_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "sessions" / "s.jsonl"
            write_jsonl(session, session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38"))
            original = Path.read_text
            reads = 0

            def counted(path_self, *args, **kwargs):
                nonlocal reads
                if path_self == session:
                    reads += 1
                return original(path_self, *args, **kwargs)

            with publisher.connect_state(root / "state") as con, mock.patch.object(Path, "read_text", counted):
                publisher.parse_session(session, con)
            self.assertEqual(1, reads)

    def test_guard_metadata_only_fingerprint_change_is_noop_but_status_change_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = self.make_git_pair(root / "pair-a")[0]
            repo_b = self.make_git_pair(root / "pair-b")[0]
            session = root / "sessions" / "s.jsonl"
            rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38")
            rows[0]["payload"]["cwd"] = str(repo_a)
            write_jsonl(session, rows)
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
                cycle = cycles[0]
                old_full = publisher._full_fingerprint(cycle)
                metrics = cycle["metrics"]
                con.execute(
                    """
                    INSERT INTO cycles (cycle_key,session_id,chat_id,final_event_id,source_path,source_sha256,cycle_sha256,published_commit,prompt_id,updated_at_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        metrics["cycle_key"],
                        metrics["native_session_id"],
                        metrics["chat_id"],
                        cycle["final_event_id"],
                        metrics["source_path"],
                        old_full,
                        old_full,
                        "abc123",
                        metrics["prompt_id"],
                        publisher.legacy.utc_stamp(),
                    ),
                )
                con.commit()
                cycle["metrics"]["repo_project"] = str(repo_b)
                cycle["metrics"]["repo_projects"] = [str(repo_b)]
                publisher._should_export = False
                publisher._guard_candidate_cycles = {}
                cycles, _events = publisher.parse_session(session, con)
                self.assertFalse(publisher._should_export)

                rows[-1]["payload"]["last_agent_message"] = "RESULT=FAIL"
                rows[-2]["payload"]["content"][0]["text"] = "RESULT=FAIL"
                write_jsonl(session, rows)
                publisher._should_export = False
                cycles, _events = publisher.parse_session(session, con)
                self.assertTrue(publisher._should_export)

    def test_fingerprint_schema_migration_is_noop_and_idempotent_until_real_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "sessions" / "s.jsonl"
            write_jsonl(session, session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38"))
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
                cycle = cycles[0]
                metrics = cycle["metrics"]
                con.execute(
                    """
                    INSERT INTO cycles (cycle_key,session_id,chat_id,prompt_id,turn_id,final_event_id,source_path,source_sha256,cycle_sha256,fingerprint_schema,published_commit,telegram_sent,updated_at_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        metrics["cycle_key"],
                        metrics["native_session_id"],
                        metrics["chat_id"],
                        metrics["prompt_id"],
                        metrics["turn_id"],
                        cycle["final_event_id"],
                        metrics["source_path"],
                        "legacy-fingerprint",
                        "legacy-fingerprint",
                        1,
                        "abc123",
                        1,
                        publisher.legacy.utc_stamp(),
                    ),
                )
                con.commit()
                publisher._should_export = False
                publisher._notify_cycle_objects = set()
                publisher.parse_session(session, con)
                self.assertFalse(publisher._should_export)
                self.assertEqual(0, len(publisher._notify_cycle_objects))
                row = con.execute("SELECT fingerprint_schema FROM cycles WHERE cycle_key=?", (metrics["cycle_key"],)).fetchone()
                self.assertEqual(publisher.FINGERPRINT_SCHEMA, row["fingerprint_schema"])

                publisher._should_export = False
                publisher.parse_session(session, con)
                self.assertFalse(publisher._should_export)

                rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38")
                rows[-2]["payload"]["content"][0]["text"] = "RESULT=PASS\nnew final evidence"
                rows[-1]["payload"]["last_agent_message"] = "RESULT=PASS\nnew final evidence"
                write_jsonl(session, rows)
                publisher._should_export = False
                publisher.parse_session(session, con)
                self.assertTrue(publisher._should_export)

    def test_workdir_only_dirty_repo_is_not_actionable_when_write_repo_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = self.make_git_pair(root / "pair-a")[0]
            repo_b = self.make_git_pair(root / "pair-b")[0]
            (repo_a / "dirty.txt").write_text("preexisting\n", encoding="utf-8")
            session = root / "sessions" / "s.jsonl"
            rows = session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38")
            rows[1]["payload"]["cwd"] = str(repo_b)
            rows.insert(
                3,
                {"timestamp": "2026-09-05T10:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "true", "workdir": str(repo_a)})}},
            )
            rows.insert(
                4,
                {
                    "timestamp": "2026-09-05T10:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "input": "*** Begin Patch\n*** Update File: file.txt\n*** End Patch",
                    },
                },
            )
            write_jsonl(session, rows)
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
                cycle = cycles[0]
                con.execute(
                    """
                    INSERT INTO cycles (cycle_key,session_id,chat_id,final_event_id,source_path,source_sha256,published_commit,updated_at_utc)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    published_cycle_sql(cycle),
                )
                recorded = publisher.record_git_completion_guard(con, cycle)
            self.assertIn(str(repo_a), cycle["metrics"]["repo_projects"])
            self.assertEqual([str(repo_b)], cycle["metrics"]["repo_write_projects"])
            self.assertEqual([str(repo_b)], [row["repo_path"] for row in recorded])
            self.assertTrue(all(row["actionable"] for row in recorded))

    def test_deploy_runtime_rejects_dirty_and_unsynced_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, bare = self.make_git_pair(root / "pair")
            for name in deploy_runtime.RUNTIME_FILES:
                (repo / name).write_text("print('ok')\n", encoding="utf-8")
            publisher.run(["git", "add", *deploy_runtime.RUNTIME_FILES], repo)
            publisher.run(["git", "commit", "-m", "runtime files"], repo)
            publisher.run(["git", "push", "origin", "main"], repo)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(deploy_runtime.DeployError, "worktree dirty"):
                deploy_runtime.assert_clean_synced(repo)
            (repo / "dirty.txt").unlink()
            (repo / "codex_usage_publisher.py").write_text("print('ahead')\n", encoding="utf-8")
            publisher.run(["git", "commit", "-am", "ahead"], repo)
            with self.assertRaisesRegex(deploy_runtime.DeployError, "HEAD is not synchronized"):
                deploy_runtime.assert_clean_synced(repo)

    def test_noop_run_does_not_invoke_guard_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sessions"
            repo = root / "repo"
            write_jsonl(source / "s.jsonl", session_rows("019fd1da-cc5d-7db1-b880-a14be6111c38"))
            publisher.run(["git", "init", "-b", "main", str(repo)])
            publisher.run(["git", "config", "user.email", "test@example.invalid"], repo)
            publisher.run(["git", "config", "user.name", "Test"], repo)
            argv = ["--source-root", str(source), "--state-dir", str(root / "state"), "--data-repo", str(repo), "run"]
            with mock.patch.object(publisher, "assert_private_repo"), mock.patch.object(publisher, "ensure_repo"), mock.patch.object(publisher, "git_ok") as git_ok, mock.patch.object(publisher, "send_batch_telegram", return_value=True), mock.patch.object(publisher, "record_git_completion_guard", wraps=publisher.record_git_completion_guard) as guard:
                git_ok.side_effect = lambda cmd, cwd, timeout=120: publisher.run(cmd, cwd)
                self.assertEqual(publisher.main(argv), 0)
                self.assertEqual(guard.call_count, 1)
                self.assertEqual(publisher.main(argv), 0)
                self.assertEqual(guard.call_count, 1)


if __name__ == "__main__":
    unittest.main()
