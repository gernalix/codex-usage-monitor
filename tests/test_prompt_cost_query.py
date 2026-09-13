from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_prompt_cost_query as query
import codex_task_costs as costs


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def token_event(ts: str, total: int, input_tokens: int, cached: int, output: int, reasoning: int) -> dict[str, object]:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": total,
                }
            },
            "rate_limits": {"primary": {"used_percent": 10.0, "resets_at": 1800000000}},
        },
    }


def native_prompt_rows(prompt_id: str, *, session: str, hour: int, end_total: int) -> list[dict[str, object]]:
    prefix = f"2026-09-13T{hour:02d}:00:"
    return [
        {"timestamp": prefix + "00Z", "type": "session_meta", "payload": {"session_id": session}},
        {"timestamp": prefix + "00.500Z", "type": "turn_context", "payload": {"model": "gpt-5.5", "collaboration_mode": {"settings": {"reasoning_effort": "low"}}}},
        token_event(prefix + "01Z", total=100, input_tokens=90, cached=80, output=10, reasoning=2),
        {
            "timestamp": prefix + "02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"PROMPT_ID={prompt_id} validate"}],
            },
        },
        {"timestamp": prefix + "03Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
        token_event(prefix + "04Z", total=end_total, input_tokens=160, cached=130, output=20, reasoning=4),
        {"timestamp": prefix + "05Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    ]


class PromptCostQueryTests(unittest.TestCase):
    def make_db(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as con:
            con.execute(
                """
                CREATE TABLE prompt_costs (
                    source_path TEXT NOT NULL,
                    prompt_seq INTEGER NOT NULL,
                    prompt_id TEXT NOT NULL,
                    completion_state TEXT,
                    first_timestamp_utc TEXT,
                    model TEXT,
                    reasoning_effort TEXT,
                    total_tokens INTEGER,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    uncached_input_tokens INTEGER,
                    output_tokens INTEGER,
                    reasoning_output_tokens INTEGER,
                    tool_call_count INTEGER,
                    duration_seconds REAL,
                    PRIMARY KEY(source_path, prompt_seq)
                )
                """
            )
            con.executemany(
                """
                INSERT INTO prompt_costs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    ("a", 1, "917364", "task_complete", "2026-09-13T12:00:00Z", "gpt-5.5", "medium", 1000, 900, 800, 100, 100, 20, 10, 30.0),
                    ("b", 1, "917364", "task_complete", "2026-09-13T13:00:00Z", "gpt-5.5", "low", 200, 180, 160, 20, 20, 2, 3, 10.0),
                    ("c", 1, "917364", "eof_incomplete", "2026-09-13T14:00:00Z", "gpt-5.5", "low", 50, 45, 40, 5, 5, 1, 1, 2.0),
                ],
            )
            con.commit()

    def test_latest_filters_without_rebuilding_or_mutating_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "task_costs.sqlite"
            self.make_db(db)
            before = (db.stat().st_size, db.stat().st_mtime_ns)

            rows = query.query_prompt_costs(db, "917364", reasoning_effort="low", latest=True)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["completion_state"], "task_complete")
            self.assertEqual(rows[0]["total_tokens"], 200)
            self.assertEqual((db.stat().st_size, db.stat().st_mtime_ns), before)

    def test_incomplete_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "task_costs.sqlite"
            self.make_db(db)

            default = query.query_prompt_costs(db, "917364", reasoning_effort="low", latest=True)
            including = query.query_prompt_costs(
                db, "917364", reasoning_effort="low", latest=True, include_incomplete=True
            )

            self.assertEqual(default[0]["total_tokens"], 200)
            self.assertEqual(including[0]["completion_state"], "eof_incomplete")
            self.assertEqual(including[0]["total_tokens"], 50)

    def test_targeted_latest_refresh_stops_after_newest_real_matching_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "archive"
            source_root = base / "sessions"
            costs.write_outputs([], [], root)
            db = root / "index/task_costs.sqlite"

            older = source_root / "older.jsonl"
            write_jsonl(older, native_prompt_rows("835917", session="older", hour=12, end_total=150))
            target = source_root / "target.jsonl"
            write_jsonl(target, native_prompt_rows("835917", session="target", hour=13, end_total=180))
            unrelated = source_root / "unrelated.jsonl"
            write_jsonl(
                unrelated,
                [
                    {"timestamp": "2026-09-13T14:00:00Z", "type": "session_meta", "payload": {"session_id": "other"}},
                    {"timestamp": "2026-09-13T14:00:01Z", "type": "response_item", "payload": {"type": "function_call_output", "output": "old PROMPT_ID=835917"}},
                ],
            )
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(target, ns=(2_000_000_000, 2_000_000_000))
            os.utime(unrelated, ns=(3_000_000_000, 3_000_000_000))

            result = query.refresh_prompt_costs(db, source_root, "835917", latest_only=True)
            rows = query.query_prompt_costs(db, "835917", reasoning_effort="low", latest=True)

            self.assertEqual(result["rollouts_refreshed"], 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_path"], str(target))
            self.assertEqual(rows[0]["completion_state"], "task_complete")
            self.assertEqual(rows[0]["total_tokens"], 80)
            self.assertEqual(rows[0]["uncached_input_tokens"], 20)
            self.assertEqual(rows[0]["tool_call_count"], 1)
            self.assertIn("835917", (root / "index/prompt_costs.csv").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute("SELECT count(*) FROM prompt_costs WHERE prompt_id='835917'").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT count(*) FROM session_costs").fetchone()[0], 0)

    def test_missing_index_fails_with_rebuild_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "run codex_task_costs.py"):
                query.query_prompt_costs(Path(tmp) / "missing.sqlite", "284731")


if __name__ == "__main__":
    unittest.main()
