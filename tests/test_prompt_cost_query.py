from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_prompt_cost_query as query


class PromptCostQueryTests(unittest.TestCase):
    def make_db(self, path: Path) -> None:
        with sqlite3.connect(path) as con:
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

    def test_missing_index_fails_with_rebuild_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "run codex_task_costs.py"):
                query.query_prompt_costs(Path(tmp) / "missing.sqlite", "284731")


if __name__ == "__main__":
    unittest.main()
