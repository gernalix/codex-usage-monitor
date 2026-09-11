from __future__ import annotations

import datetime as dt
import json
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

    def reading_from_payload(self, payload: dict) -> monitor.QuotaReading:
        reading = monitor.reading_from_payload(payload)
        return reading

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
            self.assertIn("Weekly remaining: 69% -> 68% (-1 pp)", quota_messages[0])
            self.assertIn("Weekly reset: 08-08-26 09:59", quota_messages[0])
            self.assertIn("Usage limit resets available: 1", quota_messages[0])
            self.assertNotIn("Weekly used", quota_messages[0])

    def test_four_weekly_remaining_changes_still_send_four_notifications(self) -> None:
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
                older_id = self.insert_reading(con, run_id, 11, 90, "2026-08-12T10:06:00Z", 0)
                self.insert_sent_quota_notification(con, older_id)
                for remaining in (89, 88, 87, 86):
                    snapshot_id = self.insert_reading(con, run_id, 11, remaining, "2026-08-12T10:06:00Z", 0)
                    monitor.dispatch_notifications(cfg, con, snapshot_id)
            self.assertEqual(len(sent_messages), 4)
            self.assertTrue(all(message.startswith("Weekly remaining:") for _title, message in sent_messages))

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
            "failure_diagnostics",
            "history",
            "latest_state",
            "quota_diagnostics",
            "quota_overview",
            "quota_temporal_metrics",
            "rate_limit_history",
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

    def test_rate_limits_by_limit_id_are_persisted_dynamically(self) -> None:
        payload = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 1780000000},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitName": "Codex",
                    "planType": "pro",
                    "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 1780000000},
                    "secondary": {"remainingPercent": 80, "windowDurationMins": 10080, "resetsAt": 1780100000},
                },
                "future_unknown": {
                    "limitId": "future_unknown",
                    "modelName": "gpt-future",
                    "limitType": "model",
                    "primary": {"usedPercent": 12.5, "remainingPercent": 87.5, "windowDurationMins": 60, "resetsAt": 1780003600},
                },
            },
            "rateLimitResetCredits": {"availableCount": 1},
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                snapshot_id = monitor.insert_snapshot(con, run_id, "ok", self.reading_from_payload(payload))
                rows = con.execute(
                    """
                    SELECT limit_id, model_name, plan_type, limit_family, window_name, window_duration_minutes, used_percent, remaining_percent, source_path
                    FROM rate_limit_snapshots
                    WHERE snapshot_id=?
                    ORDER BY limit_id, window_name
                    """,
                    (snapshot_id,),
                ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertIn(("future_unknown", "gpt-future", None, "model", "primary", 60, 12.5, 87.5, "rateLimitsByLimitId.future_unknown.primary"), [tuple(row) for row in rows])
        self.assertIn(("codex", None, "pro", None, "secondary", 10080, 20.0, 80.0, "rateLimitsByLimitId.codex.secondary"), [tuple(row) for row in rows])

    def test_sanitized_payload_json_is_complete_valid_and_redacted(self) -> None:
        payload = {
            "message": "contact person@example.com token sk-" + "a" * 30,
            "rateLimits": {
                "limitId": "codex",
                "secondary": {"usedPercent": 22, "windowDurationMins": 10080, "resetsAt": 1780100000},
            },
            "long": "x" * 1500,
            "rateLimitResetCredits": {"availableCount": 0},
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                snapshot_id = monitor.insert_snapshot(con, run_id, "ok", self.reading_from_payload(payload))
                row = con.execute("SELECT sanitized_excerpt, sanitized_payload_json FROM quota_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        decoded = json.loads(row["sanitized_payload_json"])
        self.assertEqual(decoded["long"], "x" * 1500)
        self.assertIn("[EMAIL_REDACTED]", decoded["message"])
        self.assertIn("[OPENAI_KEY_REDACTED]", decoded["message"])
        self.assertNotIn("person@example.com", row["sanitized_payload_json"])
        self.assertLess(len(row["sanitized_excerpt"]), len(row["sanitized_payload_json"]))

    def test_temporal_metrics_handle_windows_projection_and_reset(self) -> None:
        def payload(used: float, reset_at: int = 1788825600) -> dict:
            return {
                "rateLimitsByLimitId": {
                    "codex_bengalfox": {
                        "limitName": "Bengalfox",
                        "secondary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset_at},
                    }
                },
                "rateLimits": {
                    "limitId": "codex",
                    "secondary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset_at},
                },
                "rateLimitResetCredits": {"availableCount": 0},
            }

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            base = dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc)
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                for hours_ago, used in ((25, 1), (24, 2), (6, 9), (1, 12), (0, 15)):
                    monitor.insert_snapshot(
                        con,
                        run_id,
                        "ok",
                        self.reading_from_payload(payload(used)),
                        acquired_at=base - dt.timedelta(hours=hours_ago),
                    )
                row = con.execute(
                    """
                    SELECT consumption_1h_percent, consumption_6h_percent, consumption_24h_percent,
                           recent_consumption_per_hour, projected_used_at_reset_percent, metric_status
                    FROM quota_temporal_metrics
                    WHERE limit_id='codex_bengalfox' AND window_name='secondary'
                    """
                ).fetchone()
                monitor.insert_snapshot(
                    con,
                    run_id,
                    "ok",
                    self.reading_from_payload(payload(4, 1789430400)),
                    acquired_at=base + dt.timedelta(hours=1),
                )
                reset_row = con.execute(
                    """
                    SELECT consumption_1h_percent
                    FROM quota_temporal_metrics
                    WHERE limit_id='codex_bengalfox' AND window_name='secondary'
                    """
                ).fetchone()
        self.assertEqual(row["consumption_1h_percent"], 3)
        self.assertEqual(row["consumption_6h_percent"], 6)
        self.assertEqual(row["consumption_24h_percent"], 13)
        self.assertEqual(row["recent_consumption_per_hour"], 3)
        self.assertEqual(row["metric_status"], "OK")
        self.assertIsNotNone(row["projected_used_at_reset_percent"])
        self.assertIsNone(reset_row["consumption_1h_percent"])

    def test_failure_diagnostics_expose_current_streak_and_availability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            now = monitor.utc_now()
            with monitor.connect_db(cfg) as con:
                run_id = monitor.start_run(con)
                self.insert_reading(con, run_id, 10, 90, "2026-09-10T00:00:00Z", 0)
                con.execute("UPDATE quota_snapshots SET acquired_at_utc=? WHERE snapshot_id=1", (monitor.utc_stamp(now - dt.timedelta(hours=3)),))
                monitor.insert_snapshot(con, run_id, "error", None, error="temporary failure", acquired_at=now - dt.timedelta(hours=2))
                monitor.insert_snapshot(con, run_id, "error", None, error="temporary failure", acquired_at=now - dt.timedelta(hours=1))
                row = con.execute("SELECT * FROM failure_diagnostics").fetchone()
        self.assertEqual(row["consecutive_failures"], 2)
        self.assertIsNotNone(row["last_success_at_utc"])
        self.assertIsNotNone(row["last_failure_streak_started_at_utc"])
        self.assertEqual(row["availability_24h_percent"], 33.333)

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_cfg(tmp)
            monitor.init_db(cfg)
            monitor.init_db(cfg)
            with monitor.connect_db(cfg) as con:
                cols = [row["name"] for row in con.execute("PRAGMA table_info(quota_snapshots)").fetchall()]
                child = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rate_limit_snapshots'").fetchone()
                fk = con.execute("PRAGMA foreign_key_check").fetchall()
                integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn("sanitized_payload_json", cols)
        self.assertIsNotNone(child)
        self.assertEqual(fk, [])
        self.assertEqual(integrity, "ok")


if __name__ == "__main__":
    unittest.main()
