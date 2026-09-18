from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
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
        self.assertEqual(result["avoidable_context_read_count"], 2)
        self.assertEqual(result["repeated_exact_commands"][0]["count"], 2)
        self.assertIn("duplicate_repo_paths", codes)
        self.assertIn("memory_reads", codes)
        self.assertIn("avoidable_context_reads", codes)
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

    def test_827614_shape_flags_roundtrip_and_avoidable_meta_reads(self) -> None:
        metrics = {
            "prompt_id": "827614",
            "total_tokens": 64863,
            "input_tokens": 64345,
            "cached_input_tokens": 63872,
            "uncached_input_tokens": 473,
            "output_tokens": 518,
            "reasoning_output_tokens": 227,
            "tool_call_count": 38,
            "duration_seconds": 247.025,
            "repo_paths": ["/repo"],
        }
        events = [
            {
                "tool_name": "exec_command",
                "content_text": json.dumps({"cmd": "sed -n '1,220p' /home/user/projects/codex-roadmap/README.md"}),
            },
            {
                "tool_name": "exec_command",
                "content_text": json.dumps({"cmd": "rg -n watcher /home/user/.codex/memories/MEMORY.md"}),
            },
            {
                "tool_name": "exec_command",
                "content_text": json.dumps({"cmd": "sed -n '930,980p' /home/user/.codex/memories/MEMORY.md"}),
            },
            {
                "tool_name": "exec_command",
                "content_text": json.dumps({"cmd": "sed -n '1698,1706p' /home/user/.codex/memories/MEMORY.md"}),
            },
        ]

        result = efficiency.analyze(metrics, events)
        codes = {item["code"] for item in result["findings"]}

        self.assertEqual(3, result["memory_read_count"])
        self.assertEqual(1, result["roadmap_meta_read_count"])
        self.assertEqual(4, result["avoidable_context_read_count"])
        self.assertEqual(518, result["output_tokens"])
        self.assertEqual(227, result["reasoning_output_tokens"])
        self.assertAlmostEqual(247.025, result["duration_seconds"])
        self.assertIn("memory_reads", codes)
        self.assertIn("roadmap_meta_reads", codes)
        self.assertIn("avoidable_context_reads", codes)
        self.assertIn("roundtrip_heavy_cached_session", codes)
        self.assertNotIn("high_tool_call_count", codes)

    def test_814627_shape_flags_large_output_journal_and_status_mismatch(self) -> None:
        metrics = {
            "prompt_id": "814627",
            "status": "UNKNOWN",
            "total_tokens": 92133,
            "input_tokens": 91346,
            "cached_input_tokens": 90496,
            "uncached_input_tokens": 850,
            "output_tokens": 787,
            "reasoning_output_tokens": 386,
            "tool_call_count": 35,
            "tool_calls_by_type": {"exec_command": 32, "apply_patch": 3},
            "duration_seconds": 223.991,
            "repo_paths": ["/repo"],
            "final_response_redacted": "CAUSE:\nnot yet captured\n\nSTATUS:\nWAITING_FOR_EVENT",
        }
        events = [
            {
                "tool_name": "exec_command",
                "content_text": json.dumps({"cmd": "rg -n power ~/.codex/memories/MEMORY.md"}),
            },
            {
                "tool_name": "exec_command",
                "content_text": json.dumps(
                    {
                        "cmd": "journalctl -b -u tuned-ppd.service --no-pager; journalctl -b --no-pager | rg -i 'power|profile|thermal'"
                    }
                ),
            },
            {
                "subtype": "function_call_output",
                "content_text": "Chunk ID: a\nOriginal token count: 13705\nWarning: truncated output (original token count: 13705)\nOutput:\n...",
            },
            {
                "subtype": "function_call_output",
                "content_text": "Chunk ID: b\nOriginal token count: 6834\nOutput:\nfocused tuned log",
            },
        ]

        result = efficiency.analyze(metrics, events)
        codes = {item["code"] for item in result["findings"]}

        self.assertAlmostEqual(90496 / 91346, result["cache_ratio"])
        self.assertAlmostEqual(35 * 60 / 223.991, result["tool_calls_per_minute"])
        self.assertEqual(1, result["memory_read_count"])
        self.assertEqual(1, result["broad_boot_journal_scan_count"])
        self.assertEqual(2, result["large_tool_output_count"])
        self.assertEqual(13705, result["max_tool_output_tokens"])
        self.assertEqual(20539, result["tool_output_tokens_observed"])
        self.assertEqual(1, result["truncated_tool_output_count"])
        self.assertEqual("UNKNOWN", result["stored_status"])
        self.assertEqual("WAITING_FOR_EVENT", result["reported_status"])
        self.assertEqual({"exec_command": 32, "apply_patch": 3}, result["tool_calls_by_type"])
        self.assertIn("memory_reads", codes)
        self.assertIn("broad_boot_journal_scans", codes)
        self.assertIn("large_tool_outputs", codes)
        self.assertIn("truncated_tool_outputs", codes)
        self.assertIn("high_tool_call_rate", codes)
        self.assertIn("roundtrip_heavy_cached_session", codes)
        self.assertIn("status_parse_mismatch", codes)
        self.assertNotIn("high_tool_call_count", codes)
        self.assertNotIn("high_uncached_input_tokens", codes)

    def test_aggregate_attempts_reports_retry_roundtrip_churn(self) -> None:
        rows = [
            {
                "prompt_id": "364208",
                "completion_state": "task_complete",
                "first_timestamp_utc": "2026-09-16T03:08:07Z",
                "model": "gpt-5.5",
                "reasoning_effort": "low",
                "total_tokens": 35821,
                "input_tokens": 35440,
                "cached_input_tokens": 33152,
                "uncached_input_tokens": 2288,
                "output_tokens": 381,
                "reasoning_output_tokens": 163,
                "tool_call_count": 13,
                "duration_seconds": 60.0,
            },
            {
                "prompt_id": "364208",
                "completion_state": "task_complete",
                "first_timestamp_utc": "2026-09-16T03:33:12Z",
                "model": "gpt-5.5",
                "reasoning_effort": "low",
                "total_tokens": 40667,
                "input_tokens": 40242,
                "cached_input_tokens": 39296,
                "uncached_input_tokens": 946,
                "output_tokens": 425,
                "reasoning_output_tokens": 158,
                "tool_call_count": 16,
                "duration_seconds": 182.0,
            },
            {
                "prompt_id": "364208",
                "completion_state": "task_complete",
                "first_timestamp_utc": "2026-09-16T03:51:24Z",
                "model": "gpt-5.5",
                "reasoning_effort": "low",
                "total_tokens": 32694,
                "input_tokens": 32473,
                "cached_input_tokens": 31104,
                "uncached_input_tokens": 1369,
                "output_tokens": 221,
                "reasoning_output_tokens": 0,
                "tool_call_count": 10,
                "duration_seconds": 89.19,
            },
        ]

        result = efficiency.aggregate_attempts(rows)
        codes = {item["code"] for item in result["findings"]}

        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(result["total_tokens"], 109182)
        self.assertEqual(result["input_tokens"], 108155)
        self.assertEqual(result["cached_input_tokens"], 103552)
        self.assertEqual(result["uncached_input_tokens"], 4603)
        self.assertEqual(result["output_tokens"], 1027)
        self.assertEqual(result["reasoning_output_tokens"], 321)
        self.assertEqual(result["tool_call_count"], 39)
        self.assertAlmostEqual(result["duration_seconds"], 331.19)
        self.assertAlmostEqual(result["cache_ratio"], 103552 / 108155)
        self.assertIn("multiple_prompt_attempts", codes)
        self.assertIn("multi_attempt_roundtrip_churn", codes)

    def test_load_prompt_attempts_excludes_incomplete_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "task_costs.sqlite"
            with closing(sqlite3.connect(db)) as con:
                con.execute(
                    """
                    CREATE TABLE prompt_costs (
                        source_path TEXT NOT NULL,
                        prompt_seq INTEGER NOT NULL,
                        prompt_id TEXT NOT NULL,
                        completion_state TEXT,
                        first_timestamp_utc TEXT,
                        total_tokens INTEGER,
                        PRIMARY KEY(source_path, prompt_seq)
                    )
                    """
                )
                con.executemany(
                    "INSERT INTO prompt_costs VALUES (?,?,?,?,?,?)",
                    [
                        ("b", 1, "364208", "task_complete", "2026-09-16T03:33:12Z", 40667),
                        ("a", 1, "364208", "task_complete", "2026-09-16T03:08:07Z", 35821),
                        ("c", 1, "364208", "eof_incomplete", "2026-09-16T03:40:00Z", 100),
                        ("d", 1, "999999", "task_complete", "2026-09-16T03:50:00Z", 50),
                    ],
                )
                con.commit()

            rows = efficiency.load_prompt_attempts(db, "364208")

            self.assertEqual([row["source_path"] for row in rows], ["a", "b"])
            self.assertEqual([row["total_tokens"] for row in rows], [35821, 40667])

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
