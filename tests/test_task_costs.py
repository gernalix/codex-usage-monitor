from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_task_costs as costs


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def token_event(ts: str, *, input_tokens: int, cached: int, output: int, reasoning: int, total: int, quota: float) -> dict[str, object]:
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
            "rate_limits": {"primary": {"used_percent": quota, "resets_at": 1800000000}},
        },
    }


class TaskCostTests(unittest.TestCase):
    def test_multiple_prompt_ids_get_token_deltas_not_session_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            rows: list[dict[str, object]] = [
                {"timestamp": "2026-09-13T12:00:00Z", "type": "session_meta", "payload": {"session_id": "s1", "cwd": "/repo"}},
                {"timestamp": "2026-09-13T12:00:00.500Z", "type": "turn_context", "payload": {"model": "gpt-5.5", "collaboration_mode": {"settings": {"reasoning_effort": "low"}}}},
                token_event("2026-09-13T12:00:01Z", input_tokens=100, cached=40, output=20, reasoning=5, total=120, quota=10.0),
                {"timestamp": "2026-09-13T12:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "PROMPT_ID=111 first"}},
                {"timestamp": "2026-09-13T12:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
                {"timestamp": "2026-09-13T12:00:04Z", "type": "response_item", "payload": {"type": "function_call_output", "output": "old PROMPT_ID=999 must not create a task"}},
                token_event("2026-09-13T12:00:05Z", input_tokens=250, cached=150, output=50, reasoning=10, total=300, quota=10.2),
                {"timestamp": "2026-09-13T12:00:06Z", "type": "event_msg", "payload": {"type": "task_complete"}},
                {"timestamp": "2026-09-13T12:01:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "PROMPT_ID=222 second"}},
                {"timestamp": "2026-09-13T12:01:01Z", "type": "response_item", "payload": {"type": "reasoning", "summary": []}},
                {"timestamp": "2026-09-13T12:01:02Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
                token_event("2026-09-13T12:01:03Z", input_tokens=500, cached=400, output=80, reasoning=20, total=580, quota=10.5),
            ]
            write_jsonl(path, rows)

            session, prompts = costs.analyze_with_prompts(path)

            self.assertEqual(session["prompt_ids"], "111,222")
            self.assertEqual(len(prompts), 2)
            first, second = prompts
            self.assertEqual(first["prompt_id"], "111")
            self.assertEqual(first["input_tokens"], 150)
            self.assertEqual(first["cached_input_tokens"], 110)
            self.assertEqual(first["uncached_input_tokens"], 40)
            self.assertEqual(first["output_tokens"], 30)
            self.assertEqual(first["reasoning_output_tokens"], 5)
            self.assertEqual(first["total_tokens"], 180)
            self.assertEqual(first["tool_call_count"], 1)
            self.assertAlmostEqual(first["quota_delta_points"], 0.2)
            self.assertEqual(second["prompt_id"], "222")
            self.assertEqual(second["input_tokens"], 250)
            self.assertEqual(second["cached_input_tokens"], 250)
            self.assertEqual(second["uncached_input_tokens"], 0)
            self.assertEqual(second["output_tokens"], 30)
            self.assertEqual(second["reasoning_output_tokens"], 10)
            self.assertEqual(second["total_tokens"], 280)
            self.assertEqual(second["tool_call_count"], 1)
            self.assertEqual(second["reasoning_item_count"], 1)
            self.assertAlmostEqual(second["quota_delta_points"], 0.3)

    def test_write_outputs_persists_prompt_costs_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            path = Path(tmp) / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    {"timestamp": "2026-09-13T12:00:00Z", "type": "session_meta", "payload": {"session_id": "s1"}},
                    {"timestamp": "2026-09-13T12:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "PROMPT_ID=284731 gate"}},
                    token_event("2026-09-13T12:00:02Z", input_tokens=123, cached=100, output=17, reasoning=3, total=140, quota=11.0),
                ],
            )
            session, prompts = costs.analyze_with_prompts(path)
            costs.write_outputs([session], prompts, root)

            db = root / "index/task_costs.sqlite"
            with sqlite3.connect(db) as con:
                row = con.execute("SELECT prompt_id,total_tokens,uncached_input_tokens FROM prompt_costs").fetchone()
            self.assertEqual(row, ("284731", 140, 23))
            self.assertTrue((root / "index/prompt_costs.csv").exists())
            self.assertIn("284731", (root / "index/prompt_costs.csv").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
