from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import assert_published_prompt as assertion


class AssertPublishedPromptTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        prompt = root / "prompts/529184"
        prompt.mkdir(parents=True)
        (prompt / "metrics.json").write_text(
            json.dumps(
                {
                    "schema": "codex-usage.prompt-metrics.v1",
                    "prompt_id": "529184",
                    "native_session_id": "01a0a93b-8941-7d52-b53e-7f5523e84275",
                    "status": "PASS",
                    "completion_state": "goal_complete_turn_aborted",
                    "prompt_id_source": "goal_objective_output",
                    "total_tokens": 126386,
                    "duration_seconds": 473.0,
                }
            ),
            encoding="utf-8",
        )
        (prompt / "transcript.jsonl").write_text(
            '{"content_text":"PROMPT_ID=529184 goal_complete_turn_aborted"}\n',
            encoding="utf-8",
        )
        return root

    def test_canonical_metrics_and_transcript_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(Path(tmp))
            result = assertion.assert_published_prompt(
                root,
                "529184",
                expectations=[
                    ("native_session_id", "01a0a93b-8941-7d52-b53e-7f5523e84275"),
                    ("status", "PASS"),
                    ("total_tokens", 126386),
                    ("duration_seconds", 473.0),
                ],
                transcript_contains=["PROMPT_ID=529184", "goal_complete_turn_aborted"],
                transcript_not_contains=["PROMPT_ID=643918"],
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mismatches"], [])
        self.assertTrue(result["transcript_path"].endswith("prompts/529184/transcript.jsonl"))

    def test_unknown_session_id_field_fails_with_canonical_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(Path(tmp))
            with self.assertRaisesRegex(
                RuntimeError,
                r"unknown metrics field 'session_id'; did you mean 'native_session_id'\?",
            ):
                assertion.assert_published_prompt(
                    root,
                    "529184",
                    expectations=[("session_id", "01a0a93b-8941-7d52-b53e-7f5523e84275")],
                )

    def test_value_mismatch_is_reported_without_schema_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(Path(tmp))
            result = assertion.assert_published_prompt(
                root,
                "529184",
                expectations=[("status", "BLOCKED")],
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatches"][0]["field"], "status")

    def test_cli_value_parser_preserves_strings_and_numbers(self) -> None:
        self.assertEqual(assertion.parse_expectation("status=PASS"), ("status", "PASS"))
        self.assertEqual(assertion.parse_expectation("total_tokens=126386"), ("total_tokens", 126386))
        self.assertEqual(assertion.parse_expectation("duration_seconds=473.0"), ("duration_seconds", 473.0))


if __name__ == "__main__":
    unittest.main()
