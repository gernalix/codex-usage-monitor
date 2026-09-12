from __future__ import annotations

import unittest

import codex_usage_monitor as monitor


class QuotaNotificationPolicyTests(unittest.TestCase):
    def test_full_quota_ignores_metadata_churn(self) -> None:
        previous = {
            "weekly_remaining": "100",
            "weekly_reset": "12-09-26 18:00",
            "usage_limit_resets_available": "2",
        }
        current = {
            "weekly_remaining": "100",
            "weekly_reset": "12-09-26 18:15",
            "usage_limit_resets_available": "1",
        }
        self.assertTrue(monitor.quota_change_is_noop(previous, current))

    def test_full_quota_consumption_is_not_noop(self) -> None:
        previous = {
            "weekly_remaining": "100",
            "weekly_reset": "12-09-26 18:00",
            "usage_limit_resets_available": "2",
        }
        for remaining in ("99", "98"):
            with self.subTest(remaining=remaining):
                current = {
                    "weekly_remaining": remaining,
                    "weekly_reset": "19-09-26 18:00",
                    "usage_limit_resets_available": "2",
                }
                self.assertFalse(monitor.quota_change_is_noop(previous, current))

    def test_real_reset_to_full_is_not_noop(self) -> None:
        previous = {
            "weekly_remaining": "89",
            "weekly_reset": "12-09-26 18:00",
            "usage_limit_resets_available": "2",
        }
        current = {
            "weekly_remaining": "100",
            "weekly_reset": "19-09-26 18:00",
            "usage_limit_resets_available": "2",
        }
        self.assertFalse(monitor.quota_change_is_noop(previous, current))


if __name__ == "__main__":
    unittest.main()
