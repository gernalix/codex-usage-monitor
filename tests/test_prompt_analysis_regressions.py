from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import analyze_prompt_efficiency as efficiency
from scripts import analyze_published_prompt as published


class PromptAnalysisRegressionTests(unittest.TestCase):
    def test_exec_tool_inputs_and_lifecycle_churn_are_detected(self) -> None:
        command = "rg -n watcher /home/user/.codex/memories/MEMORY.md"
        metrics = {
            "prompt_id": "374862",
            "input_tokens": 164035,
            "cached_input_tokens": 161024,
            "uncached_input_tokens": 3011,
            "output_tokens": 372,
            "reasoning_output_tokens": 133,
            "total_tokens": 164407,
            "tool_call_count": 94,
            "duration_seconds": 1219.266,
        }
        events = [
            {
                "subtype": "custom_tool_call",
                "tool_name": "exec",
                "content_text": json.dumps({"cmd": command}),
            },
            {
                "subtype": "custom_tool_call",
                "tool_name": "exec",
                "content_text": json.dumps({"cmd": command}),
            },
            {
                "subtype": "custom_tool_call_output",
                "content_text": (
                    "Script error: cannot create a new goal because this thread has an unfinished goal; "
                    "complete the existing goal first"
                ),
            },
            {
                "subtype": "custom_tool_call_output",
                "content_text": "prompt_identity_mismatch:selected=681247:requested=374862",
            },
        ]

        result = efficiency.analyze(metrics, events)
        codes = {item["code"] for item in result["findings"]}

        self.assertEqual(result["memory_read_count"], 2)
        self.assertEqual(result["goal_start_conflict_count"], 1)
        self.assertEqual(result["roadmap_identity_mismatch_count"], 1)
        self.assertEqual(result["repeated_exact_commands"][0]["count"], 2)
        self.assertIn("roundtrip_heavy_cached_session", codes)
        self.assertIn("goal_start_conflict", codes)
        self.assertIn("roadmap_identity_mismatch", codes)

    def test_primary_work_cycle_and_structured_goal_total_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index").mkdir()
            prompt_dir = root / "prompts/374862"
            cycles_dir = prompt_dir / "cycles"
            main_dir = cycles_dir / "main-cycle"
            followup_dir = cycles_dir / "followup-cycle"
            main_dir.mkdir(parents=True)
            followup_dir.mkdir(parents=True)

            index_rows = [
                {
                    "prompt_id": "374862",
                    "path": "prompts/374862",
                    "timestamp_end_utc": "2026-09-16T10:20:00Z",
                },
                {
                    "prompt_id": "374862",
                    "path": "prompts/374862",
                    "timestamp_end_utc": "2026-09-16T10:21:00Z",
                },
            ]
            (root / "index/prompts.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in index_rows),
                encoding="utf-8",
            )

            main_metrics = {
                "prompt_id": "374862",
                "cycle_key": "main-cycle",
                "status": "BLOCKED",
                "native_session_id": "session-1",
                "timestamp_start_utc": "2026-09-16T10:00:00Z",
                "timestamp_end_utc": "2026-09-16T10:20:00Z",
                "total_tokens": 164407,
                "input_tokens": 164035,
                "cached_input_tokens": 161024,
                "uncached_input_tokens": 3011,
                "output_tokens": 372,
                "reasoning_output_tokens": 133,
                "tool_call_count": 94,
                "duration_seconds": 1219.266,
            }
            followup_metrics = {
                "prompt_id": "374862",
                "cycle_key": "followup-cycle",
                "status": "PASS",
                "native_session_id": "session-1",
                "timestamp_start_utc": "2026-09-16T10:20:01Z",
                "timestamp_end_utc": "2026-09-16T10:21:00Z",
                "total_tokens": 169185,
                "input_tokens": 168908,
                "cached_input_tokens": 168320,
                "uncached_input_tokens": 588,
                "output_tokens": 277,
                "reasoning_output_tokens": 170,
                "tool_call_count": 3,
                "duration_seconds": 30.688,
            }
            goal_event = {
                "subtype": "custom_tool_call_output",
                "content_text": json.dumps(
                    {
                        "goal": {
                            "threadId": "goal-1",
                            "tokensUsed": 190549,
                            "timeUsedSeconds": 1198,
                        }
                    }
                ),
            }
            for directory, metrics, events in (
                (main_dir, main_metrics, []),
                (followup_dir, followup_metrics, [goal_event]),
            ):
                (directory / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
                (directory / "transcript.jsonl").write_text(
                    "".join(json.dumps(event) + "\n" for event in events),
                    encoding="utf-8",
                )

            prompt_dir.mkdir(exist_ok=True)
            (prompt_dir / "metrics.json").write_text(json.dumps(followup_metrics), encoding="utf-8")
            (prompt_dir / "transcript.jsonl").write_text(json.dumps(goal_event) + "\n", encoding="utf-8")

            native_chunk = root / "native-sessions/session-1/sources/source-a/chunks/000001.jsonl"
            native_chunk.parent.mkdir(parents=True)
            native_chunk.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-09-16T10:05:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "arguments": json.dumps(
                                {"cmd": "rg -n watcher /home/user/.codex/memories/MEMORY.md"}
                            ),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = published.analyze_published_prompt(root, "374862")

        codes = {item["code"] for item in result["findings"]}
        self.assertEqual(result["selected_cycle_key"], "main-cycle")
        self.assertEqual(result["terminal_cycle_key"], "followup-cycle")
        self.assertEqual(result["selected_source"], "published_cycle")
        self.assertEqual(result["stored_cycle_count"], 2)
        self.assertEqual(result["historical_unique_cycle_count"], 2)
        self.assertEqual(result["native_tool_input_event_count"], 1)
        self.assertTrue(result["native_session_dump_used"])
        self.assertEqual(result["memory_read_count"], 1)
        self.assertEqual(result["goal_reported_tokens"], 190549)
        self.assertEqual(result["goal_reported_time_seconds"], 1198.0)
        self.assertEqual(result["aggregate_substantive"]["total_tokens"], 333592)
        self.assertIn("terminal_followup_not_primary_work_cycle", codes)
        self.assertIn("cycle_token_sum_overcounts_goal_context", codes)
        self.assertIn("roundtrip_heavy_cached_session", codes)


if __name__ == "__main__":
    unittest.main()
