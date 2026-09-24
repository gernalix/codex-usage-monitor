from __future__ import annotations

import json
from contextlib import closing
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
                # Deliberate idle gap: it must not inflate prompt 111 duration.
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
            self.assertEqual(first["completion_state"], "task_complete")
            self.assertEqual(first["duration_seconds"], 4.0)
            self.assertEqual(first["input_tokens"], 150)
            self.assertEqual(first["cached_input_tokens"], 110)
            self.assertEqual(first["uncached_input_tokens"], 40)
            self.assertEqual(first["output_tokens"], 30)
            self.assertEqual(first["reasoning_output_tokens"], 5)
            self.assertEqual(first["total_tokens"], 180)
            self.assertEqual(first["tool_call_count"], 1)
            self.assertAlmostEqual(first["quota_delta_points"], 0.2)
            self.assertEqual(second["prompt_id"], "222")
            self.assertEqual(second["completion_state"], "eof_incomplete")
            self.assertEqual(second["duration_seconds"], 3.0)
            self.assertEqual(second["input_tokens"], 250)
            self.assertEqual(second["cached_input_tokens"], 250)
            self.assertEqual(second["uncached_input_tokens"], 0)
            self.assertEqual(second["output_tokens"], 30)
            self.assertEqual(second["reasoning_output_tokens"], 10)
            self.assertEqual(second["total_tokens"], 280)
            self.assertEqual(second["tool_call_count"], 1)
            self.assertEqual(second["reasoning_item_count"], 1)
            self.assertAlmostEqual(second["quota_delta_points"], 0.3)
            self.assertEqual(costs.select_prompt_rows(prompts, "222"), [])
            self.assertEqual(len(costs.select_prompt_rows(prompts, "222", include_incomplete=True)), 1)

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
            with closing(sqlite3.connect(db)) as con:
                row = con.execute("SELECT prompt_id,completion_state,total_tokens,uncached_input_tokens FROM prompt_costs").fetchone()
            self.assertEqual(row, ("284731", "eof_incomplete", 140, 23))
            self.assertTrue((root / "index/prompt_costs.csv").exists())
            self.assertIn("284731", (root / "index/prompt_costs.csv").read_text(encoding="utf-8"))

    def test_native_response_item_user_message_creates_prompt_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    {"timestamp": "2026-09-13T12:00:00Z", "type": "session_meta", "payload": {"session_id": "s1"}},
                    {
                        "timestamp": "2026-09-13T12:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "output_text", "text": "PROMPT_ID=999 not user input"},
                                {"type": "input_text", "text": "PROMPT_ID=917364 native"},
                            ],
                        },
                    },
                    token_event("2026-09-13T12:00:02Z", input_tokens=10, cached=4, output=3, reasoning=1, total=13, quota=1.1),
                ],
            )

            _, prompts = costs.analyze_with_prompts(path)

            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["prompt_id"], "917364")
            self.assertEqual(prompts[0]["total_tokens"], 13)

    def test_native_user_message_accepts_markdown_escaped_prompt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    {"timestamp": "2026-09-18T16:54:51Z", "type": "session_meta", "payload": {"session_id": "s-escaped"}},
                    {
                        "timestamp": "2026-09-18T16:54:52Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": r"PROMPT\_ID=918274 | project\_id=49"}],
                        },
                    },
                    token_event(
                        "2026-09-18T16:54:53Z",
                        input_tokens=10,
                        cached=8,
                        output=2,
                        reasoning=1,
                        total=12,
                        quota=1.0,
                    ),
                    {"timestamp": "2026-09-18T16:54:54Z", "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )

            session, prompts = costs.analyze_with_prompts(path)

            self.assertEqual(session["prompt_ids"], "918274")
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["prompt_id"], "918274")

    def test_prompt_selection_can_disambiguate_duplicate_prompt_ids(self) -> None:
        rows = [
            {"prompt_id": "917364", "completion_state": "task_complete", "model": "gpt-5.5", "reasoning_effort": "medium", "first_timestamp_utc": "2026-09-13T11:00:00Z", "source_path": "a", "prompt_seq": 1},
            {"prompt_id": "917364", "completion_state": "task_complete", "model": "gpt-5.5", "reasoning_effort": "low", "first_timestamp_utc": "2026-09-13T12:00:00Z", "source_path": "b", "prompt_seq": 1},
            {"prompt_id": "917364", "completion_state": "eof_incomplete", "model": "gpt-5.5", "reasoning_effort": "low", "first_timestamp_utc": "2026-09-13T13:00:00Z", "source_path": "c", "prompt_seq": 2},
        ]

        selected = costs.select_prompt_rows(rows, "917364", reasoning_effort="low", latest=True)
        selected_with_partial = costs.select_prompt_rows(
            rows,
            "917364",
            reasoning_effort="low",
            latest=True,
            include_incomplete=True,
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["source_path"], "b")
        self.assertEqual(selected_with_partial[0]["source_path"], "c")


    def test_goal_continuation_uses_authoritative_roadmap_start_prompt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            rows: list[dict[str, object]] = [
                {"timestamp": "2026-09-22T18:00:00Z", "type": "session_meta", "payload": {"session_id": "goal-session"}},
                {"timestamp": "2026-09-22T18:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "PROMPT_ID=624831 legacy goal"}},
                token_event("2026-09-22T18:00:02Z", input_tokens=100, cached=80, output=10, reasoning=2, total=110, quota=1.0),
                {
                    "timestamp": "2026-09-22T18:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "output": '{"prompt_id":"613102","roadmap_status":"running","status":"ok","task_branch":"task/613102"}',
                    },
                },
                token_event("2026-09-22T18:00:04Z", input_tokens=180, cached=140, output=30, reasoning=5, total=210, quota=1.2),
                {"timestamp": "2026-09-22T18:00:05Z", "type": "event_msg", "payload": {"type": "task_complete"}},
                {
                    "timestamp": "2026-09-22T18:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": '<codex_internal_context source="goal">\nContinue working toward the active thread goal.\n</codex_internal_context>'}],
                    },
                },
                token_event("2026-09-22T18:01:01Z", input_tokens=260, cached=200, output=50, reasoning=8, total=310, quota=1.4),
                {"timestamp": "2026-09-22T18:01:02Z", "type": "event_msg", "payload": {"type": "task_complete"}},
            ]
            write_jsonl(path, rows)

            _session, prompts = costs.analyze_with_prompts(path)

            self.assertEqual([row["prompt_id"] for row in prompts], ["613102", "613102"])
            self.assertEqual([row["completion_state"] for row in prompts], ["task_complete", "task_complete"])
            self.assertEqual(sum(row["total_tokens"] for row in prompts), 310)
            self.assertEqual(costs.select_prompt_rows(prompts, "624831", include_incomplete=True), [])



if __name__ == "__main__":
    unittest.main()
