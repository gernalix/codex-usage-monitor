from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_prompt_cost_query as query
import codex_task_costs as costs
import codex_task_costs_incremental as incremental


class PromptCostStateSyncTests(unittest.TestCase):
    def test_targeted_refresh_updates_existing_incremental_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "archive"
            source_root = base / "sessions"
            source = source_root / "rollout.jsonl"
            source_root.mkdir(parents=True)
            rows = [
                {"timestamp": "2026-09-13T12:00:00Z", "type": "session_meta", "payload": {"session_id": "s1"}},
                {"timestamp": "2026-09-13T12:00:00.5Z", "type": "turn_context", "payload": {"model": "gpt-5.5", "collaboration_mode": {"settings": {"reasoning_effort": "low"}}}},
                {"timestamp": "2026-09-13T12:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "reasoning_output_tokens": 2, "total_tokens": 110}}}},
                {"timestamp": "2026-09-13T12:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "PROMPT_ID=731608 test"}]}},
                {"timestamp": "2026-09-13T12:00:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 180, "cached_input_tokens": 140, "output_tokens": 20, "reasoning_output_tokens": 3, "total_tokens": 200}}}},
                {"timestamp": "2026-09-13T12:00:04Z", "type": "event_msg", "payload": {"type": "task_complete"}},
            ]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            costs.write_outputs([], [], root)
            db = root / "index/task_costs.sqlite"
            with closing(sqlite3.connect(db)) as con:
                incremental.ensure_state_schema(con)
                con.execute(
                    "INSERT INTO cost_source_state VALUES (?,?,?)",
                    (str(source), "stale", 1),
                )
                con.commit()

            result = query.refresh_prompt_costs(db, source_root, "731608", latest_only=True)
            expected_sha, expected_size = query.source_fingerprint(source)

            self.assertEqual(result["rollouts_refreshed"], 1)
            with closing(sqlite3.connect(db)) as con:
                state = con.execute(
                    "SELECT source_sha256,source_size_bytes FROM cost_source_state WHERE source_path=?",
                    (str(source),),
                ).fetchone()
            self.assertEqual(state, (expected_sha, expected_size))


if __name__ == "__main__":
    unittest.main()
