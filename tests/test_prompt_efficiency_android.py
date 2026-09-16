from __future__ import annotations

import json
import unittest

from scripts import analyze_prompt_efficiency as efficiency


class AndroidPromptEfficiencyTests(unittest.TestCase):
    def test_815306_flags_discovery_and_verbose_success_output_without_overcalling_roundtrips(self) -> None:
        metrics = {
            "prompt_id": "815306",
            "status": "PASS",
            "total_tokens": 41186,
            "input_tokens": 41009,
            "cached_input_tokens": 40320,
            "uncached_input_tokens": 689,
            "output_tokens": 177,
            "reasoning_output_tokens": 75,
            "tool_call_count": 12,
            "duration_seconds": 74.673,
            "repo_paths": ["/repo"],
            "final_response_redacted": "RESULT=PASS",
        }
        events = [
            {
                "tool_name": "exec_command",
                "subtype": "function_call",
                "content_text": json.dumps(
                    {"cmd": "rg --files | rg 'trace_processor_shell_aarch64$|\\.pftrace$'"}
                ),
            },
            {
                "tool_name": "exec_command",
                "subtype": "function_call",
                "content_text": json.dumps({"cmd": "python3 tools/android_perfetto_query.py --help"}),
            },
            {
                "tool_name": "exec_command",
                "subtype": "function_call",
                "content_text": json.dumps(
                    {"cmd": "find benchmark/build -name trace_processor_shell_aarch64 -o -name '*.pftrace'"}
                ),
            },
            {
                "tool_name": "exec_command",
                "subtype": "function_call",
                "content_text": json.dumps({"cmd": "find /tmp -maxdepth 3 -name '*.pftrace'"}),
            },
            {
                "subtype": "function_call_output",
                "content_text": "Chunk ID: lease\nOriginal token count: 3933\nOutput:\nlease discovery",
            },
            {
                "subtype": "function_call_output",
                "content_text": (
                    "Chunk ID: gradle\nOriginal token count: 3899\nOutput:\n"
                    "> Task :app:assembleDebug\nBUILD SUCCESSFUL in 11s\n217 actionable tasks: 217 up-to-date"
                ),
            },
        ]

        result = efficiency.analyze(metrics, events)
        codes = {item["code"] for item in result["findings"]}

        self.assertAlmostEqual(40320 / 41009, result["cache_ratio"])
        self.assertEqual(4, result["trace_artifact_discovery_count"])
        self.assertEqual(2, result["large_tool_output_count"])
        self.assertEqual(3933, result["max_tool_output_tokens"])
        self.assertEqual(1, result["verbose_gradle_success_output_count"])
        self.assertIn("trace_processor_discovery_churn", codes)
        self.assertIn("large_tool_outputs", codes)
        self.assertIn("verbose_gradle_success_output", codes)
        self.assertNotIn("high_tool_call_count", codes)
        self.assertNotIn("roundtrip_heavy_cached_session", codes)


if __name__ == "__main__":
    unittest.main()
