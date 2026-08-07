from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

import codex_usage_monitor as monitor


class ResetCountParsingTests(unittest.TestCase):
    def make_cfg(self, tmp: str) -> monitor.Config:
        return monitor.Config(
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

    def insert_reading(
        self,
        con,
        run_id: int,
        used: object,
        remaining: object,
        reset: object,
        resets: object,
    ) -> int:
        digest = f"{used}:{remaining}:{reset}:{resets}"
        reading = monitor.QuotaReading(used, remaining, reset, resets, "test", digest, "", (), None)
        return monitor.insert_snapshot(con, run_id, "ok", reading)

    def insert_sent_quota_notification(self, con, snapshot_id: int) -> None:
        row = con.execute("SELECT * FROM quota_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        state = monitor.quota_notification_state(row)
        con.execute(
            """
            INSERT INTO notification_events
            (snapshot_id,event_key,event_type,title,message,decision,sent,detail,created_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                monitor.quota_state_event_key(state),
                "quota_change",
                "Codex weekly quota changed",
                "\n".join(monitor.snapshot_message_lines(row)),
                "sent",
                1,
                "test",
                monitor.utc_stamp(),
            ),
        )
        con.commit()

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

    def test_weekly_used_only_change_does_not_notify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                older_id = self.insert_reading(con, run_id, 31, 69, "2026-08-08T07:59:53Z", 1)
                self.insert_sent_quota_notification(con, older_id)
                snapshot_id = self.insert_reading(con, run_id, 32, 69, "2026-08-08T07:59:53Z", 1)
                events = monitor.build_notification_events(cfg, con, snapshot_id)
            quota_messages = [event[3] for event in events if event[1] == "quota_change"]
            self.assertEqual(quota_messages, [])

    def test_identical_notified_quota_state_does_not_notify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                older_id = self.insert_reading(con, run_id, 11, 89, "2026-08-12T10:06:00Z", 0)
                self.insert_sent_quota_notification(con, older_id)
                snapshot_id = self.insert_reading(con, run_id, 12, "89", "2026-08-12T12:06:00+02:00", "0")
                events = monitor.build_notification_events(cfg, con, snapshot_id)
            quota_messages = [event[3] for event in events if event[1] == "quota_change"]
            self.assertEqual(quota_messages, [])

    def test_weekly_remaining_change_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                older_id = self.insert_reading(con, run_id, 11, 89, "2026-08-12T10:06:00Z", 0)
                self.insert_sent_quota_notification(con, older_id)
                snapshot_id = self.insert_reading(con, run_id, 11, 88, "2026-08-12T10:06:00Z", 0)
                events = monitor.build_notification_events(cfg, con, snapshot_id)
            quota_messages = [event[3] for event in events if event[1] == "quota_change"]
            self.assertEqual(len(quota_messages), 1)

    def test_weekly_reset_change_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                older_id = self.insert_reading(con, run_id, 11, 89, "2026-08-12T10:06:00Z", 0)
                self.insert_sent_quota_notification(con, older_id)
                snapshot_id = self.insert_reading(con, run_id, 11, 89, "2026-08-12T11:06:00Z", 0)
                events = monitor.build_notification_events(cfg, con, snapshot_id)
            quota_messages = [event[3] for event in events if event[1] == "quota_change"]
            self.assertEqual(len(quota_messages), 1)

    def test_usage_limit_reset_count_change_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                older_id = self.insert_reading(con, run_id, 11, 89, "2026-08-12T10:06:00Z", 0)
                self.insert_sent_quota_notification(con, older_id)
                snapshot_id = self.insert_reading(con, run_id, 11, 89, "2026-08-12T10:06:00Z", 1)
                events = monitor.build_notification_events(cfg, con, snapshot_id)
            quota_messages = [event[3] for event in events if event[1] == "quota_change"]
            self.assertEqual(len(quota_messages), 1)

    def test_quota_change_notification_includes_relevant_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                older_id = self.insert_reading(con, run_id, 31, 69, "2026-08-08T07:59:53Z", 1)
                self.insert_sent_quota_notification(con, older_id)
                snapshot_id = self.insert_reading(con, run_id, 32, 68, "2026-08-08T07:59:53Z", 1)
                events = monitor.build_notification_events(cfg, con, snapshot_id)
            quota_messages = [event[3] for event in events if event[1] == "quota_change"]
            self.assertTrue(quota_messages)
            self.assertIn("Weekly remaining: 68%", quota_messages[0])
            self.assertIn("Weekly reset: 08-08-26 09:59", quota_messages[0])
            self.assertIn("Usage limit resets available: 1", quota_messages[0])
            self.assertNotIn("Weekly used", quota_messages[0])

    def test_repeated_dispatch_of_same_new_state_sends_at_most_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            sent_messages: list[tuple[str, str]] = []
            with monitor.connect_db(cfg) as con, mock.patch.object(
                monitor,
                "send_telegram",
                side_effect=lambda _cfg, title, message: sent_messages.append((title, message)) or "sent",
            ):
                run_id = monitor.start_run(con)
                older_id = self.insert_reading(con, run_id, 11, 89, "2026-08-12T10:06:00Z", 0)
                self.insert_sent_quota_notification(con, older_id)
                first_id = self.insert_reading(con, run_id, 11, 88, "2026-08-12T10:06:00Z", 0)
                monitor.dispatch_notifications(cfg, con, first_id)
                second_id = self.insert_reading(con, run_id, 12, 88, "2026-08-12T12:06:00+02:00", 0)
                monitor.dispatch_notifications(cfg, con, second_id)
                sent_events = con.execute(
                    "SELECT COUNT(*) FROM notification_events WHERE event_type='quota_change' AND sent=1"
                ).fetchone()[0]
            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(sent_events, 2)

    def test_snapshot_message_has_compact_telegram_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                reading = monitor.QuotaReading(32, 68, "2026-08-08T07:59:53Z", 1, "test", "format", "", (), None)
                snapshot_id = monitor.insert_snapshot(con, run_id, "ok", reading)
                row = con.execute("SELECT * FROM quota_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
                self.assertIsNotNone(row)
                message = "\n".join(monitor.snapshot_message_lines(row))
            self.assertEqual(
                message,
                "Weekly remaining: 68%\nWeekly reset: 08-08-26 09:59\nUsage limit resets available: 1",
            )

    def test_weekly_reset_display_uses_copenhagen_timezone(self) -> None:
        self.assertEqual(monitor.format_display_datetime("2026-08-12T10:06:00Z"), "12-08-26 12:06")
        self.assertEqual(monitor.format_display_datetime("2026-12-12T10:06:00Z"), "12-12-26 11:06")

    def test_init_db_creates_canonical_datasette_views(self) -> None:
        expected = {
            "history",
            "latest_state",
            "quota_diagnostics",
            "quota_overview",
            "recent_failures",
            "reset_count_changes",
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type='view'").fetchall()
            views = {row["name"]: row["sql"] for row in rows}
        self.assertEqual(set(views), expected)
        self.assertIn("CREATE VIEW quota_overview AS", views["quota_overview"])
        self.assertIn("ORDER BY acquired_at_utc DESC, snapshot_id DESC", views["quota_overview"])
        self.assertIn("CREATE VIEW quota_diagnostics AS", views["quota_diagnostics"])
        self.assertIn("source_method", views["quota_diagnostics"])

    def test_init_db_upgrades_existing_view_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                con.executescript(
                    """
                    DROP VIEW quota_overview;
                    CREATE VIEW quota_overview AS SELECT snapshot_id FROM quota_snapshots;
                    """
                )
                con.commit()

            monitor.init_db(cfg)

            with monitor.connect_db(cfg) as con:
                sql = con.execute(
                    "SELECT sql FROM sqlite_master WHERE type='view' AND name='quota_overview'"
                ).fetchone()[0]
        self.assertIn("weekly_remaining_percent", sql)
        self.assertIn("ORDER BY acquired_at_utc DESC, snapshot_id DESC", sql)
        self.assertNotIn("SELECT snapshot_id FROM quota_snapshots", sql)


if __name__ == "__main__":
    unittest.main()
