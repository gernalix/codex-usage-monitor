from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import analyze_recent_prompts as recent


def call(command: str) -> dict[str, object]:
    return {
        "subtype": "function_call",
        "tool_name": "exec_command",
        "content_text": json.dumps({"cmd": command}),
    }


def output(code: int = 0) -> dict[str, object]:
    return {
        "subtype": "function_call_output",
        "tool_name": None,
        "content_text": f"Chunk ID: x\nProcess exited with code {code}\nOutput:\nvalue",
    }


class RecentPromptEfficiencyTests(unittest.TestCase):
    def test_roundtrips_group_parallel_calls_and_detect_failures(self) -> None:
        events = [
            call("git status --short"),
            call("git rev-parse HEAD"),
            output(0),
            output(0),
            {"subtype": "reasoning", "tool_name": None, "content_text": ""},
            call("sqlite3 db 'select missing from x'"),
            output(1),
        ]

        self.assertEqual(2, recent.tool_roundtrip_count(events))
        self.assertEqual(
            [{"exit_code": 1, "command": "sqlite3 db 'select missing from x'"}],
            recent.command_outcomes(events),
        )

    def test_enrich_distinguishes_roundtrip_churn_from_waiting(self) -> None:
        churn_events = []
        for index in range(20):
            churn_events.extend([call(f"echo {index}"), output(0)])
        churn = recent.enrich(
            {
                "prompt_id": "1",
                "input_tokens": 100000,
                "cached_input_tokens": 99500,
                "uncached_input_tokens": 500,
                "tool_call_count": 20,
                "duration_seconds": 120,
                "repo_paths": ["/repo"],
            },
            churn_events,
        )
        churn_codes = {item["code"] for item in churn["findings"]}
        self.assertEqual(20, churn["tool_roundtrip_count"])
        self.assertEqual(1.0, churn["tool_calls_per_roundtrip"])
        self.assertIn("high_roundtrip_count", churn_codes)
        self.assertIn("sequential_roundtrip_churn", churn_codes)

        wait_events = [call("long-running-safe-check"), output(0)] * 5
        waiting = recent.enrich(
            {
                "prompt_id": "2",
                "input_tokens": 100000,
                "cached_input_tokens": 99800,
                "uncached_input_tokens": 200,
                "tool_call_count": 5,
                "duration_seconds": 240,
                "repo_paths": ["/repo"],
            },
            wait_events,
        )
        waiting_codes = {item["code"] for item in waiting["findings"]}
        self.assertEqual(5, waiting["tool_roundtrip_count"])
        self.assertIn("wait_dominated_session", waiting_codes)
        self.assertNotIn("high_roundtrip_count", waiting_codes)

    def test_explicit_context_guard_violation_is_reported(self) -> None:
        events = [
            {
                "subtype": "message",
                "tool_name": None,
                "content_text": "Non rileggere README, roadmap o MEMORY.md: il prompt è autosufficiente.",
            },
            call("sed -n '1,80p' /home/u/.codex/memories/MEMORY.md"),
            output(0),
        ]
        result = recent.enrich(
            {
                "prompt_id": "3",
                "input_tokens": 1000,
                "cached_input_tokens": 900,
                "uncached_input_tokens": 100,
                "tool_call_count": 1,
                "duration_seconds": 5,
                "repo_paths": ["/repo"],
            },
            events,
        )
        codes = {item["code"] for item in result["findings"]}
        self.assertTrue(result["explicit_context_guard"])
        self.assertIn("explicit_context_guard_violation", codes)

    def test_recent_ranking_uses_roundtrips_or_duration_and_aggregates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index").mkdir()
            rows = []
            fixtures = [
                ("a", "2026-09-16T03:00:00Z", 30, 120.0, 20),
                ("b", "2026-09-16T02:00:00Z", 4, 500.0, 4),
                ("c", "2026-09-16T01:00:00Z", 5, 60.0, 5),
            ]
            for prompt_id, timestamp, calls, duration, roundtrips in fixtures:
                rel = Path("prompts") / prompt_id
                target = root / rel
                target.mkdir(parents=True)
                metrics = {
                    "prompt_id": prompt_id,
                    "input_tokens": 1000,
                    "cached_input_tokens": 900,
                    "uncached_input_tokens": 100,
                    "tool_call_count": calls,
                    "duration_seconds": duration,
                    "repo_paths": ["/repo"],
                }
                (target / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
                events = []
                for index in range(roundtrips):
                    events.extend([call(f"echo {index}"), output(0)])
                (target / "transcript.jsonl").write_text(
                    "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
                )
                rows.append(
                    {
                        "prompt_id": prompt_id,
                        "path": rel.as_posix(),
                        "timestamp_end_utc": timestamp,
                    }
                )
            (root / "index/prompts.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )

            report = recent.analyze_recent(root, recent_unique=3, top=2)

        self.assertEqual(["a", "b"], [row["prompt_id"] for row in report["prompts"]])
        self.assertEqual(2000, report["totals"]["input_tokens"])
        self.assertEqual(1800, report["totals"]["cached_input_tokens"])
        self.assertEqual(200, report["totals"]["fresh_input_tokens"])
        self.assertEqual(24, report["totals"]["tool_roundtrip_count"])
        self.assertAlmostEqual(0.9, report["totals"]["cache_ratio"])


if __name__ == "__main__":
    unittest.main()
