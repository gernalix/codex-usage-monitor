"""Presentation boundary: ensure public compatibility without invoking the collector."""
import datetime as dt
import unittest
from codex_monitor.capsules.quota import implementation, presentation


class QuotaPresentationTests(unittest.TestCase):
    def test_implementation_reexports_pure_functions(self):
        for name in ("DISPLAY_TZ", "normalize_number", "fmt_value",
                     "format_display_datetime", "quota_notification_state",
                     "quota_state_event_key", "quota_state_from_message",
                     "quota_change_is_noop"):
            self.assertIs(getattr(implementation, name), getattr(presentation, name))

    def test_timezone_and_stable_event_key(self):
        self.assertEqual(presentation.format_display_datetime("2026-12-12T10:06:00Z"), "12-12-26 11:06")
        state={"weekly_remaining":"49","weekly_reset":"12-12-26 11:06","usage_limit_resets_available":"1"}
        self.assertEqual(presentation.quota_state_event_key(dict(reversed(list(state.items())))),
                         presentation.quota_state_event_key(state))
        self.assertEqual(presentation.quota_state_from_message(
            "Weekly remaining: 49%\nWeekly reset: 12-12-26 11:06\nUsage limit resets available: 1"),state)


if __name__=="__main__":
    unittest.main()
