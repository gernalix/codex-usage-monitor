from __future__ import annotations

import json
import unittest

from scripts import analyze_prompt_efficiency as efficiency


class PromptEfficiencyTests(unittest.TestCase):
    def test_reports_duplicate_paths_memory_reads_and_repeated_commands(self) -> None:
        metrics = {
            "prompt_id": "594217",
            "total_tokens": 100000,
            "input_tokens": 99000,
            "cached_input_tokens": 20000,
            "uncached_input_tokens": 79000,
            "tool_call_count": 52,
            "duration_seconds": 200,
            "repo_paths": ["/repo", "/repo", "/other"],
        }
        command = "rg -n test /home/user/.codex/memories/MEMORY.md"
        events = [
            {"tool_name": "exec_command", "content_text": json.dumps({"cmd": command})},
            {"tool_name": "exec_command", "content_text": json.dumps({"cmd": command})},
        ]

        result = efficiency.analyze(metrics, events)

        codes = {item["code"] for item in result["findings"]}
        self.assertEqual(result["repo_path_count"], 3)
        self.assertEqual(result["unique_repo_path_count"], 2)
        self.assertEqual(result["memory_read_count"], 2)
        self.assertEqual(result["repeated_exact_commands"][0]["count"], 2)
        self.assertIn("duplicate_repo_paths", codes)
        self.assertIn("memory_reads", codes)
        self.assertIn("repeated_exact_commands", codes)
        self.assertIn("high_tool_call_count", codes)
        self.assertIn("high_uncached_input_tokens", codes)
        self.assertNotIn("roundtrip_heavy_cached_session", codes)

    def test_cached_tool_heavy_android_run_is_classified_as_roundtrip_heavy(self) -> None:
        metrics = {
            "prompt_id": "734581",
            "total_tokens": 119899,
            "input_tokens": 119466,
            "cached_input_tokens": 118144,
            "uncached_input_tokens": 1322,
            "tool_call_count": 52,
            "duration_seconds": 533.509,
            "repo_paths": ["/repo"] * 10,
        }
        events = [
            {
                "tool_name": "exec_command",
                "content_text": json.dumps(
                    {
                        "cmd": "adb devices -l && for s in pixel emulator; do adb -s $s shell getprop ro.product.model; done && adb install -r /tmp/46.apk"
                    }
                ),
            },
            {"tool_name": "exec_command", "content_text": json.dumps({"cmd": "which trace_processor_shell"})},
            {"tool_name": "exec_command", "content_text": json.dumps({"cmd": "find /opt -name trace_processor_shell"})},
            {"tool_name": "exec_command", "content_text": json.dumps({"cmd": "adb shell /tmp/trace_processor -Q 'select 1' trace.pftrace"})},
        ]

        result = efficiency.analyze(metrics, events)
        codes = {item["code"] for item in result["findings"]}

        self.assertAlmostEqual(118144 / 119466, result["cache_ratio"])
        self.assertAlmostEqual(1322 / 119466, result["uncached_input_share"])
        self.assertEqual(1, result["unscoped_adb_install_count"])
        self.assertEqual(3, result["trace_processor_command_count"])
        self.assertIn("duplicate_repo_paths", codes)
        self.assertIn("high_tool_call_count", codes)
        self.assertIn("roundtrip_heavy_cached_session", codes)
        self.assertIn("unscoped_adb_install", codes)
        self.assertIn("trace_processor_discovery_churn", codes)
        self.assertNotIn("high_uncached_input_tokens", codes)

    def test_serial_scoped_adb_install_is_not_flagged(self) -> None:
        result = efficiency.analyze(
            {"prompt_id": "1", "input_tokens": 1000, "cached_input_tokens": 0, "tool_call_count": 3, "repo_paths": ["/repo"]},
            [
                {
                    "tool_name": "exec_command",
                    "content_text": json.dumps({"cmd": "adb -s pixel-serial install -r /tmp/46.apk"}),
                }
            ],
        )
        codes = {item["code"] for item in result["findings"]}
        self.assertNotIn("unscoped_adb_install", codes)

    def test_clean_small_prompt_has_no_findings(self) -> None:
        result = efficiency.analyze(
            {
                "prompt_id": "1",
                "input_tokens": 1000,
                "cached_input_tokens": 0,
                "uncached_input_tokens": 1000,
                "tool_call_count": 3,
                "repo_paths": ["/repo"],
            },
            [],
        )
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
