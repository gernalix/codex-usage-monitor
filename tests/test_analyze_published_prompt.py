from __future__ import annotations

import json
from pathlib import Path
import subprocess
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

    def test_git_history_recovers_substantive_cycle_after_trivial_followup_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            (root / "index").mkdir()
            prompt_dir = root / "prompts/893806"
            prompt_dir.mkdir(parents=True)
            index_rows = (
                {
                    "prompt_id": "893806",
                    "path": "prompts/893806",
                    "timestamp_end_utc": "2026-09-16T07:23:23Z",
                },
                {
                    "prompt_id": "893806",
                    "path": "prompts/893806",
                    "timestamp_end_utc": "2026-09-16T07:29:01Z",
                },
            )
            (root / "index/prompts.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in index_rows),
                encoding="utf-8",
            )
            substantive = {
                "prompt_id": "893806",
                "cycle_key": "substantive",
                "status": "PASS",
                "timestamp_end_utc": "2026-09-16T07:23:23Z",
                "total_tokens": 146413,
                "input_tokens": 146145,
                "cached_input_tokens": 145792,
                "uncached_input_tokens": 353,
                "output_tokens": 268,
                "reasoning_output_tokens": 73,
                "tool_call_count": 101,
                "duration_seconds": 1927.151,
            }
            (prompt_dir / "metrics.json").write_text(json.dumps(substantive), encoding="utf-8")
            (prompt_dir / "transcript.jsonl").write_text(
                json.dumps({"subtype": "function_call", "tool_name": "exec_command", "content_text": "{}"}) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "substantive"], cwd=root, check=True)

            trivial = {
                "prompt_id": "893806",
                "cycle_key": "trivial-followup",
                "status": "UNKNOWN",
                "timestamp_end_utc": "2026-09-16T07:29:01Z",
                "total_tokens": 142794,
                "input_tokens": 142784,
                "cached_input_tokens": 28032,
                "uncached_input_tokens": 114752,
                "output_tokens": 10,
                "reasoning_output_tokens": 0,
                "tool_call_count": 0,
                "duration_seconds": 3.389,
                "prompt_text_redacted": "prompt id?",
            }
            (prompt_dir / "metrics.json").write_text(json.dumps(trivial), encoding="utf-8")
            (prompt_dir / "transcript.jsonl").write_text("", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "followup"], cwd=root, check=True)

            result = published.analyze_published_prompt(root, "893806")

        codes = {item["code"] for item in result["findings"]}
        self.assertEqual(result["selected_cycle_key"], "substantive")
        self.assertEqual(result["current_cycle_key"], "trivial-followup")
        self.assertTrue(result["selected_from_git_history"])
        self.assertEqual(result["historical_unique_cycle_count"], 2)
        self.assertEqual(result["aggregate_substantive"]["substantive_cycle_count"], 1)
        self.assertEqual(result["aggregate_substantive"]["total_tokens"], 146413)
        self.assertEqual(result["aggregate_substantive"]["tool_call_count"], 101)
        self.assertIn("latest_prompt_files_overwrote_substantive_cycle", codes)

    def test_missing_prompt_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index").mkdir()
            (root / "index/prompts.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "PROMPT_ID=999999 not found"):
                published.analyze_published_prompt(root, "999999")


if __name__ == "__main__":
    unittest.main()
