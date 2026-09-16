from __future__ import annotations

import json
import unittest

from scripts import analyze_prompt_efficiency as efficiency


class PromptEfficiencyFailureTests(unittest.TestCase):
    def test_467281_shape_flags_failed_schema_and_retry_churn(self) -> None:
        metrics = {
            "prompt_id": "467281",
            "total_tokens": 107341,
            "input_tokens": 106747,
            "cached_input_tokens": 105856,
            "uncached_input_tokens": 891,
            "output_tokens": 594,
            "reasoning_output_tokens": 320,
            "tool_call_count": 84,
            "duration_seconds": 337.251,
            "repo_paths": ["/repo"],
        }
        events = [
            {"tool_name": "exec_command", "content_text": json.dumps({"cmd": "sqlite3 db '.tables'"})},
            {"tool_name": "exec_command", "content_text": json.dumps({"cmd": "sqlite3 db 'pragma table_info(projects)'"})},
            {"tool_name": "exec_command", "content_text": json.dumps({"cmd": "sqlite3 db 'pragma table_info(kuma_monitors)'"})},
            {"subtype": "function_call_output", "content_text": "Process exited with code 8\nError: attempt to write a readonly database"},
            {"subtype": "function_call_output", "content_text": "Process exited with code 8\nError: attempt to write a readonly database"},
            {"subtype": "function_call_output", "content_text": "Process exited with code 1\nError: no such column: id"},
            {"subtype": "function_call_output", "content_text": "Process exited with code 1\nTraceback:\nModuleNotFoundError: No module named 'fedora_system_monitor'"},
        ]

        result = efficiency.analyze(metrics, events)
        codes = {item["code"] for item in result["findings"]}

        self.assertEqual(4, result["failed_command_count"])
        self.assertEqual(2, result["sqlite_readonly_failure_count"])
        self.assertEqual(1, result["python_import_failure_count"])
        self.assertEqual(3, result["schema_probe_command_count"])
        self.assertIn("failed_command_churn", codes)
        self.assertIn("sqlite_readonly_retry", codes)
        self.assertIn("python_import_retry", codes)
        self.assertIn("schema_discovery_churn", codes)
        self.assertIn("high_tool_call_count", codes)
        self.assertIn("roundtrip_heavy_cached_session", codes)

    def test_single_failed_command_is_not_classified_as_churn(self) -> None:
        result = efficiency.analyze(
            {"prompt_id": "1", "input_tokens": 1000, "cached_input_tokens": 0, "tool_call_count": 2},
            [{"subtype": "function_call_output", "content_text": "Process exited with code 1\nexpected probe failure"}],
        )
        codes = {item["code"] for item in result["findings"]}
        self.assertEqual(1, result["failed_command_count"])
        self.assertNotIn("failed_command_churn", codes)


if __name__ == "__main__":
    unittest.main()
