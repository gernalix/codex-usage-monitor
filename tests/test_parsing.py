from __future__ import annotations

import unittest

import codex_usage_monitor as monitor


class ResetCountParsingTests(unittest.TestCase):
    def test_structured_one_reset(self) -> None:
        payload = {"rateLimitResetCredits": {"availableCount": 1, "credits": [{"status": "available"}]}}
        value, warnings = monitor.reset_count_from_payload(payload)
        self.assertEqual(value, 1)
        self.assertEqual(warnings, [])

    def test_structured_multiple_resets(self) -> None:
        payload = {"rateLimitResetCredits": {"availableCount": "3"}}
        value, _warnings = monitor.reset_count_from_payload(payload)
        self.assertEqual(value, 3)

    def test_structured_zero_resets(self) -> None:
        payload = {"rateLimitResetCredits": {"availableCount": 0, "credits": []}}
        value, _warnings = monitor.reset_count_from_payload(payload)
        self.assertEqual(value, 0)

    def test_missing_reset_field(self) -> None:
        value, warnings = monitor.reset_count_from_payload({})
        self.assertIsNone(value)
        self.assertIn("structured reset credit object missing", warnings)

    def test_malformed_reset_message(self) -> None:
        self.assertIsNone(monitor.parse_reset_count_from_text("resets available: lots"))

    def test_text_singular_reset(self) -> None:
        self.assertEqual(monitor.parse_reset_count_from_text("You have 1 usage limit reset available."), 1)
        self.assertEqual(monitor.parse_reset_count_from_text("One free reset available"), 1)

    def test_text_plural_resets(self) -> None:
        self.assertEqual(monitor.parse_reset_count_from_text("Usage limit resets available: 2"), 2)
        self.assertEqual(monitor.parse_reset_count_from_text("3 free rate limit resets remaining"), 3)

    def test_text_zero_resets(self) -> None:
        self.assertEqual(monitor.parse_reset_count_from_text("No usage limit resets available"), 0)
        self.assertEqual(monitor.parse_reset_count_from_text("0 resets remaining"), 0)

    def test_weekly_window_from_primary_or_secondary(self) -> None:
        payload = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 41, "windowDurationMins": 300, "resetsAt": 1780000000},
                "secondary": {"usedPercent": 22, "windowDurationMins": 10080, "resetsAt": 1780100000},
            }
        }
        used, remaining, reset, warnings = monitor.choose_weekly_window(payload)
        self.assertEqual(used, 22)
        self.assertEqual(remaining, 78)
        self.assertEqual(reset, "2026-05-30T00:13:20Z")
        self.assertEqual(warnings, [])

    def test_no_weekly_window_never_invents(self) -> None:
        used, remaining, reset, warnings = monitor.choose_weekly_window({"rateLimits": {"primary": None}})
        self.assertIsNone(used)
        self.assertIsNone(remaining)
        self.assertIsNone(reset)
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
