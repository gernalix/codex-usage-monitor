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

    def test_clean_small_prompt_has_no_findings(self) -> None:
        result = efficiency.analyze(
            {
                "prompt_id": "1",
                "uncached_input_tokens": 1000,
                "tool_call_count": 3,
                "repo_paths": ["/repo"],
            },
            [],
        )
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
