from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

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

    def test_quota_change_notification_includes_reset_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = monitor.Config(
                db_path=Path(tmp) / "codex_usage_monitor.db",
                state_dir=Path(tmp),
                lock_path=Path(tmp) / "lock",
                codex_bin="codex",
                app_server_port=38655,
                source_timeout_sec=5,
                startup_timeout_sec=5,
                sqlite_timeout_sec=5,
                telegram_helper=Path(tmp) / "telegram_notify.py",
                telegram_enabled=True,
                notify_approaching_expiry_hours=12,
                notify_failure_after_runs=3,
                notification_cooldown_minutes=60,
            )
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                older = monitor.QuotaReading(31, 69, "2026-08-08T07:59:53Z", 1, "test", "older", "", (), None)
                current = monitor.QuotaReading(32, 68, "2026-08-08T07:59:53Z", 1, "test", "current", "", (), None)
                monitor.insert_snapshot(con, run_id, "ok", older)
                snapshot_id = monitor.insert_snapshot(con, run_id, "ok", current)
                events = monitor.build_notification_events(cfg, con, snapshot_id)
            quota_messages = [event[3] for event in events if event[1] == "quota_change"]
            self.assertTrue(quota_messages)
            self.assertIn("usage limit resets available 1", quota_messages[0])

    def test_reset_count_change_triggers_distinct_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = monitor.Config(
                db_path=Path(tmp) / "codex_usage_monitor.db",
                state_dir=Path(tmp),
                lock_path=Path(tmp) / "lock",
                codex_bin="codex",
                app_server_port=38655,
                source_timeout_sec=5,
                startup_timeout_sec=5,
                sqlite_timeout_sec=5,
                telegram_helper=Path(tmp) / "telegram_notify.py",
                telegram_enabled=True,
                notify_approaching_expiry_hours=12,
                notify_failure_after_runs=3,
                notification_cooldown_minutes=60,
            )
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                older = monitor.QuotaReading(32, 68, "2026-08-08T07:59:53Z", 1, "test", "older-reset", "", (), None)
                current = monitor.QuotaReading(32, 68, "2026-08-08T07:59:53Z", 2, "test", "current-reset", "", (), None)
                monitor.insert_snapshot(con, run_id, "ok", older)
                snapshot_id = monitor.insert_snapshot(con, run_id, "ok", current)
                events = monitor.build_notification_events(cfg, con, snapshot_id)
            reset_events = [event for event in events if event[1] == "reset_count_change"]
            self.assertEqual(len(reset_events), 1)
            self.assertEqual(reset_events[0][0], "reset_count_change:1->2")
            self.assertIn("Usage limit resets available 1 -> 2", reset_events[0][3])


if __name__ == "__main__":
    unittest.main()
