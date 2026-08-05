from __future__ import annotations

import gzip
import json
import tempfile
from pathlib import Path
import unittest

import codex_session_archive as archive


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class SessionArchiveTests(unittest.TestCase):
    def make_rows(self, session_id: str, *, complete: bool = True) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = [
            {
                "timestamp": "2026-08-05T10:00:00.000Z",
                "type": "session_meta",
                "payload": {"session_id": session_id, "cwd": "/tmp", "cli_version": "0.test"},
            },
            {
                "timestamp": "2026-08-05T10:00:01.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "PROMPT_ID=4 run command"},
            },
            {
                "timestamp": "2026-08-05T10:00:02.000Z",
                "type": "response_item",
                "payload": {"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\":\"printf token 123456:" + "abcdefghijklmnopqrstuvwxyz\"}", "call_id": "call_1"},
            },
            {
                "timestamp": "2026-08-05T10:00:03.000Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call_1", "output": "Output:\nhello\n"},
            },
        ]
        if complete:
            rows.append(
                {
                    "timestamp": "2026-08-05T10:00:04.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn_1"},
                }
            )
        return rows

    def test_import_session_preserves_raw_and_writes_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            source = Path(tmp) / "sessions"
            session_id = "019fd1da-cc5d-7db1-b880-a14be6111c38"
            src = source / "2026/08/05" / f"rollout-2026-08-05T12-00-00-{session_id}.jsonl"
            write_jsonl(src, self.make_rows(session_id))

            imported_id, did_import, manifest = archive.import_session(root, source, src)

            self.assertEqual(imported_id, session_id)
            self.assertTrue(did_import)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["prompt_ids"], ["4"])
            raw_path = Path(manifest["raw_archive_path"])
            self.assertTrue(raw_path.exists())
            with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
                self.assertIn("PROMPT_ID=4", handle.read())
            norm_path = Path(manifest["normalized_path"])
            events = [json.loads(line) for line in norm_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 5)
            self.assertIn("[TELEGRAM_TOKEN_REDACTED]", events[2]["content_text"])

    def test_resume_append_updates_same_session_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            source = Path(tmp) / "sessions"
            session_id = "019fd1da-cc5d-7db1-b880-a14be6111c38"
            src = source / "rollout-2026-08-05T12-00-00-019fd1da-cc5d-7db1-b880-a14be6111c38.jsonl"
            write_jsonl(src, self.make_rows(session_id, complete=False))
            archive.import_session(root, source, src)
            with src.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": "2026-08-05T10:00:05.000Z", "type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")

            archive.import_session(root, source, src)

            with archive.connect_db(root) as con:
                rows = con.execute("SELECT session_id,event_count,status FROM sessions").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_count"], 5)
            self.assertEqual(rows[0]["status"], "complete")

    def test_incomplete_session_models_safe_abnormal_termination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            source = Path(tmp) / "sessions"
            session_id = "019fd1da-cc5d-7db1-b880-a14be6111c38"
            src = source / "rollout-2026-08-05T12-00-00-019fd1da-cc5d-7db1-b880-a14be6111c38.jsonl"
            write_jsonl(src, self.make_rows(session_id, complete=False))

            _session_id, _did_import, manifest = archive.import_session(root, source, src)

            self.assertEqual(manifest["status"], "incomplete")
            self.assertEqual(manifest["invalid_json_lines"], 0)

    def test_corrupt_line_is_classified_and_normalized_still_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            source = Path(tmp) / "sessions"
            session_id = "019fd1da-cc5d-7db1-b880-a14be6111c38"
            src = source / "rollout-2026-08-05T12-00-00-019fd1da-cc5d-7db1-b880-a14be6111c38.jsonl"
            write_jsonl(src, self.make_rows(session_id))
            with src.open("a", encoding="utf-8") as handle:
                handle.write("{not-json}\n")

            _session_id, _did_import, manifest = archive.import_session(root, source, src)

            self.assertEqual(manifest["status"], "corrupt")
            result = argparse_like(root)
            self.assertEqual(archive.command_verify(result), 0)

    def test_import_command_deduplicates_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            codex = Path(tmp) / ".codex"
            source = codex / "sessions"
            session_id = "019fd1da-cc5d-7db1-b880-a14be6111c38"
            src = source / "rollout-2026-08-05T12-00-00-019fd1da-cc5d-7db1-b880-a14be6111c38.jsonl"
            write_jsonl(src, self.make_rows(session_id))
            (codex / "history.jsonl").write_text(json.dumps({"session_id": session_id}) + "\n", encoding="utf-8")
            args = argparse_like(root, source_root=source, codex_dir=codex)

            self.assertEqual(archive.command_import(args), 0)
            self.assertEqual(archive.command_import(args), 0)

            with archive.connect_db(root) as con:
                rows = con.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
                runs = con.execute("SELECT sessions_imported,sessions_skipped FROM import_runs ORDER BY run_id").fetchall()
            self.assertEqual(rows["n"], 1)
            self.assertEqual(runs[0]["sessions_imported"], 1)
            self.assertEqual(runs[1]["sessions_imported"], 0)
            self.assertEqual(runs[1]["sessions_skipped"], 1)

    def test_distinct_rollout_files_with_same_session_id_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            source = Path(tmp) / "sessions"
            session_id = "019fd1da-cc5d-7db1-b880-a14be6111c38"
            first = source / "rollout-2026-08-05T12-00-00-019fd1da-cc5d-7db1-b880-a14be6111c38.jsonl"
            second = source / "rollout-2026-08-05T12-01-00-019fd1db-1111-7222-8333-444444444444.jsonl"
            write_jsonl(first, self.make_rows(session_id))
            write_jsonl(second, self.make_rows(session_id))

            archive.import_session(root, source, first)
            archive.import_session(root, source, second)

            with archive.connect_db(root) as con:
                rows = con.execute("SELECT archive_id,session_id,normalized_path FROM sessions ORDER BY archive_id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["session_id"] for row in rows}, {session_id})
            self.assertEqual(len({row["normalized_path"] for row in rows}), 2)


def argparse_like(root: Path, **kwargs: Path) -> object:
    class Args:
        pass

    args = Args()
    args.archive_root = str(root)
    args.source_root = str(kwargs.get("source_root", Path("/does/not/matter")))
    args.codex_dir = str(kwargs.get("codex_dir", Path("/does/not/matter")))
    return args


if __name__ == "__main__":
    unittest.main()
