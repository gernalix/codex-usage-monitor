"""Quota collection configuration and versioned SQLite persistence schema.

Configuration and schema can be initialized without a Codex session,
network credentials or Telegram notifications.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class Config:
    db_path: Path
    state_dir: Path
    lock_path: Path
    codex_bin: str
    app_server_port: int
    source_timeout_sec: int
    startup_timeout_sec: int
    sqlite_timeout_sec: int
    telegram_helper: Path
    telegram_enabled: bool
    notify_approaching_expiry_hours: int
    notify_failure_after_runs: int
    notification_cooldown_minutes: int


def connect_db(cfg: Config) -> sqlite3.Connection:
    import sqlite3

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg.db_path, timeout=cfg.sqlite_timeout_sec)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={cfg.sqlite_timeout_sec * 1000}")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def ensure_column(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(cfg: Config) -> None:
    with connect_db(cfg) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS acquisition_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                status TEXT NOT NULL,
                source_method TEXT NOT NULL,
                source_version TEXT NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS quota_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES acquisition_runs(run_id),
                acquired_at_utc TEXT NOT NULL,
                acquired_minute_utc TEXT NOT NULL,
                acquisition_status TEXT NOT NULL,
                source_method TEXT NOT NULL,
                source_version TEXT NOT NULL,
                source_format TEXT,
                weekly_used_percent REAL,
                weekly_remaining_percent REAL,
                weekly_reset_at_utc TEXT,
                usage_limit_resets_available INTEGER,
                sanitized_error TEXT,
                sanitized_excerpt TEXT,
                sanitized_payload_json TEXT,
                source_payload_sha256 TEXT,
                parse_warnings TEXT,
                provenance TEXT NOT NULL DEFAULT 'live',
                created_at_utc TEXT NOT NULL,
                UNIQUE(acquired_minute_utc, source_payload_sha256, acquisition_status, provenance)
            );
            CREATE INDEX IF NOT EXISTS idx_quota_snapshots_acquired ON quota_snapshots(acquired_at_utc);
            CREATE INDEX IF NOT EXISTS idx_quota_snapshots_status ON quota_snapshots(acquisition_status, acquired_at_utc);
            CREATE INDEX IF NOT EXISTS idx_quota_snapshots_run_id ON quota_snapshots(run_id);

            CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
                rate_limit_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL REFERENCES quota_snapshots(snapshot_id) ON DELETE CASCADE,
                limit_id TEXT NOT NULL,
                limit_name TEXT,
                model_name TEXT,
                plan_type TEXT,
                limit_family TEXT,
                window_name TEXT NOT NULL,
                window_duration_minutes INTEGER,
                used_percent REAL,
                remaining_percent REAL,
                reset_at_utc TEXT,
                source_path TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(snapshot_id, limit_id, window_name, source_path)
            );
            CREATE INDEX IF NOT EXISTS idx_rate_limit_snapshots_snapshot ON rate_limit_snapshots(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_rate_limit_snapshots_limit_time ON rate_limit_snapshots(limit_id, reset_at_utc);

            CREATE TABLE IF NOT EXISTS quota_attributions (
                snapshot_id INTEGER PRIMARY KEY REFERENCES quota_snapshots(snapshot_id) ON DELETE CASCADE,
                previous_snapshot_id INTEGER NOT NULL REFERENCES quota_snapshots(snapshot_id),
                diagnostic_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_events (
                notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER REFERENCES quota_snapshots(snapshot_id),
                event_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                decision TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0 CHECK(sent IN (0,1)),
                detail TEXT,
                created_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notification_events_key_created ON notification_events(event_key, created_at_utc);
            CREATE INDEX IF NOT EXISTS idx_notification_events_snapshot_id ON notification_events(snapshot_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_events_quota_v2_sent_key ON notification_events(event_key) WHERE sent=1 AND event_key LIKE 'quota_change:v2:%';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_events_quota_v2_sending_key ON notification_events(event_key) WHERE decision='sending' AND event_key LIKE 'quota_change:v2:%';

            CREATE TABLE IF NOT EXISTS imports (
                import_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                imported_at_utc TEXT NOT NULL,
                rows_imported INTEGER NOT NULL,
                source_sha256 TEXT,
                backup_path TEXT
            );
            """
        )
        ensure_column(con, "quota_snapshots", "sanitized_payload_json", "TEXT")
        ensure_column(con, "quota_snapshots", "reset_credits_json", "TEXT")
        con.executescript(
            """

            DROP VIEW IF EXISTS latest_state;
            DROP VIEW IF EXISTS history;
            DROP VIEW IF EXISTS reset_count_changes;
            DROP VIEW IF EXISTS recent_failures;
            DROP VIEW IF EXISTS quota_overview;
            DROP VIEW IF EXISTS quota_diagnostics;
            DROP VIEW IF EXISTS rate_limit_history;
            DROP VIEW IF EXISTS quota_temporal_metrics;
            DROP VIEW IF EXISTS failure_diagnostics;

            CREATE VIEW latest_state AS
            SELECT *
            FROM quota_snapshots
            ORDER BY acquired_at_utc DESC, snapshot_id DESC
            LIMIT 1;

            CREATE VIEW history AS
            SELECT snapshot_id, acquired_at_utc, acquisition_status, weekly_used_percent,
                   weekly_remaining_percent, weekly_reset_at_utc,
                   usage_limit_resets_available, reset_credits_json, source_format, provenance,
                   sanitized_error, parse_warnings
            FROM quota_snapshots
            ORDER BY acquired_at_utc DESC, snapshot_id DESC;

            CREATE VIEW reset_count_changes AS
            WITH ordered AS (
                SELECT snapshot_id, acquired_at_utc, usage_limit_resets_available,
                       LAG(usage_limit_resets_available) OVER (ORDER BY acquired_at_utc, snapshot_id) AS previous_resets
                FROM quota_snapshots
                WHERE acquisition_status = 'ok'
            )
            SELECT *
            FROM ordered
            WHERE usage_limit_resets_available IS NOT NULL
              AND previous_resets IS NOT NULL
              AND usage_limit_resets_available != previous_resets
            ORDER BY acquired_at_utc DESC, snapshot_id DESC;

            CREATE VIEW recent_failures AS
            SELECT snapshot_id, acquired_at_utc, acquisition_status, sanitized_error, parse_warnings, source_format
            FROM quota_snapshots
            WHERE acquisition_status != 'ok'
            ORDER BY acquired_at_utc DESC, snapshot_id DESC
            LIMIT 50;

            CREATE VIEW quota_overview AS
            SELECT acquired_at_utc,
                   weekly_used_percent,
                   weekly_remaining_percent,
                   weekly_reset_at_utc,
                   usage_limit_resets_available,
                   acquisition_status
            FROM quota_snapshots
            ORDER BY acquired_at_utc DESC, snapshot_id DESC;

            CREATE VIEW quota_diagnostics AS
            SELECT snapshot_id,
                   run_id,
                   acquired_at_utc,
                   source_method,
                   source_version,
                   source_format,
                   acquisition_status,
                   sanitized_error,
                   parse_warnings,
                   provenance
            FROM quota_snapshots
            ORDER BY acquired_at_utc DESC, snapshot_id DESC;

            CREATE VIEW rate_limit_history AS
            SELECT qs.snapshot_id,
                   qs.acquired_at_utc,
                   qs.acquisition_status,
                   qs.provenance,
                   rls.limit_id,
                   rls.limit_name,
                   rls.model_name,
                   rls.plan_type,
                   rls.limit_family,
                   rls.window_name,
                   rls.window_duration_minutes,
                   rls.used_percent,
                   rls.remaining_percent,
                   rls.reset_at_utc,
                   rls.source_path
            FROM rate_limit_snapshots rls
            JOIN quota_snapshots qs ON qs.snapshot_id = rls.snapshot_id
            ORDER BY qs.acquired_at_utc DESC, qs.snapshot_id DESC, rls.limit_id, rls.window_name;

            CREATE VIEW quota_temporal_metrics AS
            WITH samples AS (
                SELECT snapshot_id, acquired_at_utc, 'weekly_general' AS limit_id, 'secondary' AS window_name,
                       10080 AS window_duration_minutes, weekly_used_percent AS used_percent,
                       weekly_remaining_percent AS remaining_percent, weekly_reset_at_utc AS reset_at_utc
                FROM quota_snapshots
                WHERE acquisition_status = 'ok' AND weekly_used_percent IS NOT NULL
                UNION ALL
                SELECT qs.snapshot_id, qs.acquired_at_utc, rls.limit_id, rls.window_name,
                       rls.window_duration_minutes, rls.used_percent, rls.remaining_percent, rls.reset_at_utc
                FROM rate_limit_snapshots rls
                JOIN quota_snapshots qs ON qs.snapshot_id = rls.snapshot_id
                WHERE qs.acquisition_status = 'ok' AND rls.used_percent IS NOT NULL
            ),
            latest AS (
                SELECT *
                FROM (
                    SELECT samples.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY limit_id, window_name, window_duration_minutes
                               ORDER BY acquired_at_utc DESC, snapshot_id DESC
                           ) AS rn
                    FROM samples
                )
                WHERE rn = 1
            ),
            anchors AS (
                SELECT latest.*,
                       (
                           SELECT used_percent FROM samples s
                           WHERE s.limit_id = latest.limit_id
                             AND s.window_name = latest.window_name
                             AND COALESCE(s.window_duration_minutes, -1) = COALESCE(latest.window_duration_minutes, -1)
                             AND s.reset_at_utc = latest.reset_at_utc
                             AND julianday(s.acquired_at_utc) <= julianday(latest.acquired_at_utc) - (1.0 / 24.0)
                           ORDER BY s.acquired_at_utc DESC, s.snapshot_id DESC LIMIT 1
                       ) AS used_1h,
                       (
                           SELECT used_percent FROM samples s
                           WHERE s.limit_id = latest.limit_id
                             AND s.window_name = latest.window_name
                             AND COALESCE(s.window_duration_minutes, -1) = COALESCE(latest.window_duration_minutes, -1)
                             AND s.reset_at_utc = latest.reset_at_utc
                             AND julianday(s.acquired_at_utc) <= julianday(latest.acquired_at_utc) - (6.0 / 24.0)
                           ORDER BY s.acquired_at_utc DESC, s.snapshot_id DESC LIMIT 1
                       ) AS used_6h,
                       (
                           SELECT used_percent FROM samples s
                           WHERE s.limit_id = latest.limit_id
                             AND s.window_name = latest.window_name
                             AND COALESCE(s.window_duration_minutes, -1) = COALESCE(latest.window_duration_minutes, -1)
                             AND s.reset_at_utc = latest.reset_at_utc
                             AND julianday(s.acquired_at_utc) <= julianday(latest.acquired_at_utc) - 1.0
                           ORDER BY s.acquired_at_utc DESC, s.snapshot_id DESC LIMIT 1
                       ) AS used_24h
                FROM latest
            ),
            computed AS (
                SELECT *,
                       CASE WHEN used_1h IS NOT NULL AND used_percent >= used_1h THEN used_percent - used_1h END AS consumption_1h_percent,
                       CASE WHEN used_6h IS NOT NULL AND used_percent >= used_6h THEN used_percent - used_6h END AS consumption_6h_percent,
                       CASE WHEN used_24h IS NOT NULL AND used_percent >= used_24h THEN used_percent - used_24h END AS consumption_24h_percent,
                       CASE
                           WHEN reset_at_utc IS NOT NULL AND window_duration_minutes IS NOT NULL
                           THEN (julianday(acquired_at_utc) - (julianday(reset_at_utc) - (window_duration_minutes / 1440.0))) * 24.0
                       END AS elapsed_hours,
                       CASE WHEN reset_at_utc IS NOT NULL THEN (julianday(reset_at_utc) - julianday(acquired_at_utc)) * 24.0 END AS remaining_hours
                FROM anchors
            )
            SELECT limit_id,
                   window_name,
                   snapshot_id AS latest_snapshot_id,
                   acquired_at_utc AS latest_acquired_at_utc,
                   reset_at_utc,
                   window_duration_minutes,
                   used_percent,
                   remaining_percent,
                   consumption_1h_percent,
                   consumption_6h_percent,
                   consumption_24h_percent,
                   CASE WHEN elapsed_hours > 0 THEN used_percent / elapsed_hours END AS average_consumption_per_hour_since_reset,
                   COALESCE(consumption_1h_percent, consumption_6h_percent / 6.0, consumption_24h_percent / 24.0) AS recent_consumption_per_hour,
                   CASE
                       WHEN elapsed_hours BETWEEN 0 AND (window_duration_minutes / 60.0)
                       THEN ROUND(MIN(100.0, MAX(0.0, 100.0 * elapsed_hours / (window_duration_minutes / 60.0))), 3)
                   END AS ideal_used_percent,
                   CASE
                       WHEN elapsed_hours BETWEEN 0 AND (window_duration_minutes / 60.0)
                       THEN ROUND(used_percent - MIN(100.0, MAX(0.0, 100.0 * elapsed_hours / (window_duration_minutes / 60.0))), 3)
                   END AS delta_vs_ideal_percent,
                   CASE
                       WHEN remaining_hours >= 0
                       THEN ROUND(MIN(100.0, MAX(0.0, used_percent + COALESCE(consumption_1h_percent, consumption_6h_percent / 6.0, consumption_24h_percent / 24.0, CASE WHEN elapsed_hours > 0 THEN used_percent / elapsed_hours END) * remaining_hours)), 3)
                   END AS projected_used_at_reset_percent,
                   CASE
                       WHEN reset_at_utc IS NULL OR window_duration_minutes IS NULL THEN 'UNKNOWN'
                       WHEN elapsed_hours < 0 OR remaining_hours < 0 THEN 'UNKNOWN'
                       ELSE 'OK'
                   END AS metric_status
            FROM computed
            ORDER BY limit_id, window_name, window_duration_minutes;

            CREATE VIEW failure_diagnostics AS
            WITH ordered AS (
                SELECT snapshot_id, acquired_at_utc, acquisition_status,
                       ROW_NUMBER() OVER (ORDER BY acquired_at_utc DESC, snapshot_id DESC) AS rn
                FROM quota_snapshots
            ),
            first_ok AS (
                SELECT MIN(rn) AS rn FROM ordered WHERE acquisition_status = 'ok'
            ),
            current_failures AS (
                SELECT * FROM ordered
                WHERE acquisition_status != 'ok'
                  AND rn < COALESCE((SELECT rn FROM first_ok), 1000000000)
            ),
            last_24h AS (
                SELECT acquisition_status
                FROM quota_snapshots
                WHERE julianday(acquired_at_utc) >= julianday('now') - 1
            )
            SELECT (SELECT MAX(acquired_at_utc) FROM quota_snapshots WHERE acquisition_status = 'ok') AS last_success_at_utc,
                   (SELECT COUNT(*) FROM current_failures) AS consecutive_failures,
                   (SELECT MIN(acquired_at_utc) FROM current_failures) AS last_failure_streak_started_at_utc,
                   CASE
                       WHEN (SELECT COUNT(*) FROM current_failures) > 0
                       THEN ROUND((julianday('now') - julianday((SELECT MIN(acquired_at_utc) FROM current_failures))) * 24.0 * 60.0, 3)
                   END AS last_failure_streak_minutes,
                   CASE
                       WHEN (SELECT COUNT(*) FROM last_24h) > 0
                       THEN ROUND(100.0 * (SELECT COUNT(*) FROM last_24h WHERE acquisition_status = 'ok') / (SELECT COUNT(*) FROM last_24h), 3)
                   END AS availability_24h_percent;
            """
        )
        con.commit()
