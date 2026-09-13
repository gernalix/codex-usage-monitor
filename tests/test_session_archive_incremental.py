from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_session_archive_incremental as incremental


def write_rollout(path: Path, session_id: str, prompt_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": "2026-09-13T12:00:00Z", "type": "session_meta", "payload": {"session_id": session_id, "cwd": None}},
        {"timestamp": "2026-09-13T12:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"PROMPT_ID={prompt_id} test"}]}},
        {"timestamp": "2026-09-13T12:00:02Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class IncrementalArchiveTests(unittest.TestCase):
    def test_unchanged_rollouts_are_not_snapshotted_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "archive"
            source = base / "codex/sessions"
            codex_dir = base / "codex"
            first = source / "first.jsonl"
            second = source / "second.jsonl"
            write_rollout(first, "s1", "111")
            write_rollout(second, "s2", "222")

            initial = incremental.incremental_import(root, source, codex_dir)
            first_raw_mtime = next((root / "raw/sessions").rglob("first.jsonl.gz")).stat().st_mtime_ns
            unchanged = incremental.incremental_import(root, source, codex_dir)

            self.assertEqual(initial["sessions_candidates"], 2)
            self.assertEqual(initial["sessions_imported"], 2)
            self.assertEqual(unchanged["sessions_candidates"], 0)
            self.assertEqual(unchanged["sessions_imported"], 0)
            self.assertEqual(next((root / "raw/sessions").rglob("first.jsonl.gz")).stat().st_mtime_ns, first_raw_mtime)

    def test_only_grown_rollout_is_reimported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "archive"
            source = base / "codex/sessions"
            codex_dir = base / "codex"
            first = source / "first.jsonl"
            second = source / "second.jsonl"
            write_rollout(first, "s1", "111")
            write_rollout(second, "s2", "222")
            incremental.incremental_import(root, source, codex_dir)
            with first.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": "2026-09-13T12:00:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 1}}}}) + "\n")

            result = incremental.incremental_import(root, source, codex_dir)

            self.assertEqual(result["sessions_candidates"], 1)
            self.assertEqual(result["sessions_imported"], 1)
            self.assertEqual(result["sessions_skipped"], 1)
            with closing(sqlite3.connect(root / "index/archive.sqlite")) as con:
                sizes = dict(con.execute("SELECT session_id,source_size_bytes FROM sessions"))
            self.assertEqual(sizes["s1"], first.stat().st_size)
            self.assertEqual(sizes["s2"], second.stat().st_size)


if __name__ == "__main__":
    unittest.main()
