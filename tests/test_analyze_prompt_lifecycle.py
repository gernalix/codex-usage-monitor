from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import analyze_prompt_lifecycle as lifecycle


class PromptLifecycleAnalysisTests(unittest.TestCase):
    @staticmethod
    def _write_cycle(root: Path, key: str, metrics: dict[str, object]) -> None:
        cycle = root / "prompts/681247/cycles" / key
        cycle.mkdir(parents=True)
        (cycle / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
        (cycle / "transcript.jsonl").write_text("", encoding="utf-8")

    def test_fail_to_pass_retry_reports_efficiency_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index").mkdir()
            (root / "index/prompts.jsonl").write_text(
                json.dumps({"prompt_id": "681247", "path": "prompts/681247/cycles/retry", "timestamp_end_utc": "2026-09-16T14:22:30Z"}) + "\n",
                encoding="utf-8",
            )
            common = {
                "prompt_id": "681247",
                "chat_id": 274,
                "native_session_id": "same-session",
                "model": "gpt-5.5",
                "reasoning_effort": "low",
            }
            self._write_cycle(
                root,
                "initial",
                {
                    **common,
                    "cycle_key": "initial",
                    "status": "FAIL",
                    "timestamp_end_utc": "2026-09-16T14:07:59Z",
                    "duration_seconds": 93.585,
                    "total_tokens": 32160,
                    "input_tokens": 31794,
                    "cached_input_tokens": 30080,
                    "uncached_input_tokens": 1714,
                    "output_tokens": 366,
                    "reasoning_output_tokens": 137,
                    "tool_call_count": 13,
                    "cache_ratio": 0.9460904573,
                },
            )
            self._write_cycle(
                root,
                "retry",
                {
                    **common,
                    "cycle_key": "retry",
                    "status": "PASS",
                    "timestamp_end_utc": "2026-09-16T14:22:30Z",
                    "duration_seconds": 88.646,
                    "total_tokens": 35731,
                    "input_tokens": 35580,
                    "cached_input_tokens": 35200,
                    "uncached_input_tokens": 380,
                    "output_tokens": 151,
                    "reasoning_output_tokens": 0,
                    "tool_call_count": 5,
                    "cache_ratio": 0.9893198426,
                },
            )

            result = lifecycle.analyze_lifecycle(root, "681247")

        self.assertEqual(result["status_lifecycle"], ["FAIL", "PASS"])
        self.assertEqual(result["status_transition"], "FAIL -> PASS")
        self.assertTrue(result["continued_in_same_chat"])
        self.assertEqual(result["terminal_vs_first"]["tool_call_count"]["delta"], -8.0)
        self.assertEqual(result["terminal_vs_first"]["uncached_input_tokens"]["delta"], -1334.0)
        self.assertEqual(result["terminal_vs_first"]["output_tokens"]["delta"], -215.0)
        self.assertEqual(result["terminal_vs_first"]["total_tokens"]["delta"], 3571.0)
        proxy = result["terminal_vs_first"]["uncached_plus_output_tokens"]
        self.assertEqual(proxy["before"], 2080)
        self.assertEqual(proxy["after"], 531)
        self.assertEqual(proxy["delta"], -1549)


if __name__ == "__main__":
    unittest.main()
