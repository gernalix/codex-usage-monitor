from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import codex_usage_publisher as publisher


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def session_rows(session_id: str, prompt_id: str = "123456", second_prompt: str | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"timestamp": "2026-09-05T10:00:00Z", "type": "session_meta", "payload": {"session_id": session_id, "cwd": "/tmp"}},
        {"timestamp": "2026-09-05T10:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-test", "collaboration_mode": {"settings": {"reasoning_effort": "low"}}}},
        {"timestamp": "2026-09-05T10:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": f"PROMPT_ID: {prompt_id} do it"}]}},
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


class UsagePublisherTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
