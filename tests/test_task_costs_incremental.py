from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_task_costs_incremental as incremental


def write_rollout(path: Path, prompt_id: str, end_total: int) -> None:
    rows = [
        {"timestamp": "2026-09-13T12:00:00Z", "type": "session_meta", "payload": {"session_id": path.stem}},
        {"timestamp": "2026-09-13T12:00:00.5Z", "type": "turn_context", "payload": {"model": "gpt-5.5", "collaboration_mode": {"settings": {"reasoning_effort": "low"}}}},
        {"timestamp": "2026-09-13T12:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "reasoning_output_tokens": 2, "total_tokens": 110}}}},
        {"timestamp": "2026-09-13T12:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"PROMPT_ID={prompt_id} test"}]}},
        {"timestamp": "2026-09-13T12:00:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": end_total - 20, "cached_input_tokens": 100, "output_tokens": 20, "reasoning_output_tokens": 3, "total_tokens": end_total}}}},
        {"timestamp": "2026-09-13T12:00:04Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def fingerprint(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def make_archive_index(root: Path, sources: list[Path]) -> None:
    db = root / "index/archive.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE sessions(source_path TEXT PRIMARY KEY,source_sha256 TEXT,source_size_bytes INTEGER)")
        con.executemany(
            "INSERT INTO sessions VALUES (?,?,?)",
            [(str(path), *fingerprint(path)) for path in sources],
        )
        con.commit()


def update_archive_source(root: Path, path: Path) -> None:
    sha, size = fingerprint(path)
    with closing(sqlite3.connect(root / "index/archive.sqlite")) as con:
        con.execute(
            "UPDATE sessions SET source_sha256=?,source_size_bytes=? WHERE source_path=?",
            (sha, size, str(path)),
        )
        con.commit()


class IncrementalCostTests(unittest.TestCase):
    def test_bootstrap_then_no_change_parses_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            first = Path(tmp) / "sessions/first.jsonl"
            second = Path(tmp) / "sessions/second.jsonl"
            write_rollout(first, "111", 180)
            write_rollout(second, "222", 190)
            make_archive_index(root, [first, second])

            boot = incremental.incremental_update(root)
            before_db = (root / "index/task_costs.sqlite").stat().st_mtime_ns
            before_csv = (root / "index/task_costs.csv").stat().st_mtime_ns
            unchanged = incremental.incremental_update(root)

            self.assertEqual(boot["mode"], "bootstrap")
            self.assertEqual(boot["sources_parsed"], 2)
            self.assertEqual(unchanged["mode"], "incremental")
            self.assertEqual(unchanged["sources_parsed"], 0)
            self.assertEqual((root / "index/task_costs.sqlite").stat().st_mtime_ns, before_db)
            self.assertEqual((root / "index/task_costs.csv").stat().st_mtime_ns, before_csv)

    def test_only_changed_archived_rollout_is_reparsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            first = Path(tmp) / "sessions/first.jsonl"
            second = Path(tmp) / "sessions/second.jsonl"
            write_rollout(first, "111", 180)
            write_rollout(second, "222", 190)
            make_archive_index(root, [first, second])
            incremental.incremental_update(root)

            write_rollout(second, "222", 260)
            update_archive_source(root, second)
            result = incremental.incremental_update(root)

            self.assertEqual(result["mode"], "incremental")
            self.assertEqual(result["sources_parsed"], 1)
            with closing(sqlite3.connect(root / "index/task_costs.sqlite")) as con:
                totals = dict(con.execute("SELECT prompt_id,total_tokens FROM prompt_costs"))
                state_count = con.execute("SELECT COUNT(*) FROM cost_source_state").fetchone()[0]
            self.assertEqual(totals["111"], 70)
            self.assertEqual(totals["222"], 150)
            self.assertEqual(state_count, 2)

    def test_removed_archive_source_removes_only_its_cost_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            first = Path(tmp) / "sessions/first.jsonl"
            second = Path(tmp) / "sessions/second.jsonl"
            write_rollout(first, "111", 180)
            write_rollout(second, "222", 190)
            make_archive_index(root, [first, second])
            incremental.incremental_update(root)
            with closing(sqlite3.connect(root / "index/archive.sqlite")) as con:
                con.execute("DELETE FROM sessions WHERE source_path=?", (str(second),))
                con.commit()

            result = incremental.incremental_update(root)

            self.assertEqual(result["sources_removed"], 1)
            with closing(sqlite3.connect(root / "index/task_costs.sqlite")) as con:
                ids = [row[0] for row in con.execute("SELECT prompt_id FROM prompt_costs")]
            self.assertEqual(ids, ["111"])


if __name__ == "__main__":
    unittest.main()
