from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import analyze_published_prompt as published


class PublishedPromptAnalysisTests(unittest.TestCase):
    def test_loads_latest_cycle_metrics_transcript_and_goal_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index").mkdir()
            prompt_dir = root / "prompts/812604"
            prompt_dir.mkdir(parents=True)
            (root / "index/prompts.jsonl").write_text(
                "".join(
                    json.dumps(row) + "\n"
                    for row in (
                        {
                            "prompt_id": "812604",
                            "path": "prompts/812604",
                            "timestamp_end_utc": "2026-09-16T04:40:00Z",
                        },
                        {
                            "prompt_id": "812604",
                            "path": "prompts/812604",
                            "timestamp_end_utc": "2026-09-16T04:41:09Z",
                        },
                    )
                ),
                encoding="utf-8",
            )
            (prompt_dir / "metrics.json").write_text(
                json.dumps(
                    {
                        "prompt_id": "812604",
                        "total_tokens": 104989,
                        "input_tokens": 104810,
                        "cached_input_tokens": 103808,
                        "uncached_input_tokens": 1002,
                        "tool_call_count": 51,
                        "duration_seconds": 277.019,
                        "repo_paths": ["/repo"],
                    }
                ),
                encoding="utf-8",
            )
            events = [
                {
                    "subtype": "function_call",
                    "tool_name": "exec_command",
                    "content_text": json.dumps({"cmd": "sed -n '1,160p' /repo/missing.py"}),
                },
                {
                    "subtype": "function_call_output",
                    "tool_name": None,
                    "content_text": "Process exited with code 2\nNo such file or directory",
                },
                {
                    "subtype": "function_call_output",
                    "tool_name": None,
                    "content_text": json.dumps(
                        {"goal": {"tokensUsed": 113743, "timeUsedSeconds": 271, "status": "complete"}}
                    ),
                },
            ]
            (prompt_dir / "transcript.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

            result = published.analyze_published_prompt(root, "812604")

        codes = {item["code"] for item in result["findings"]}
        self.assertEqual(result["prompt_id"], "812604")
        self.assertEqual(result["published_cycle_count"], 2)
        self.assertTrue(result["transcript_available"])
        self.assertEqual(result["timestamp_end_utc"], "2026-09-16T04:41:09Z")
        self.assertEqual(result["failed_command_count"], 1)
        self.assertEqual(result["goal_reported_tokens"], 113743)
        self.assertEqual(result["goal_reported_time_seconds"], 271.0)
        self.assertEqual(result["goal_vs_metrics_token_delta"], 8754)
        self.assertIn("roundtrip_heavy_cached_session", codes)
        self.assertIn("goal_metrics_token_delta", codes)
        self.assertIn("published_prompt_has_multiple_cycles", codes)

    def test_missing_prompt_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index").mkdir()
            (root / "index/prompts.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "PROMPT_ID=999999 not found"):
                published.analyze_published_prompt(root, "999999")


if __name__ == "__main__":
    unittest.main()
