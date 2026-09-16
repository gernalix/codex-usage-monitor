#!/usr/bin/env python3
"""Compatibility adapter for the quota capsule."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.quota.api import (
    Config, QuotaReading, build_notification_events, choose_weekly_window, connect_db,
    dispatch_notifications, format_display_datetime, init_db, insert_snapshot, main,
    parse_reset_count_from_text, quota_change_is_noop, quota_notification_state,
    quota_state_event_key, reading_from_payload, reset_count_from_payload,
    snapshot_message_lines, start_run, utc_now, utc_stamp,
)
if __name__ == "__main__":
    raise SystemExit(main())
