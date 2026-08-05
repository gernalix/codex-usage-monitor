# codex-usage-monitor

Independent Oracle VM monitor for Codex quota observations.

The monitor reads the existing Codex app-server JSON-RPC method
`account/rateLimits/read`, stores append-only observations in SQLite, and lets
the existing Datasette service expose the database and views from
`/home/ubuntu/sync_root/db`.

Persistent application state lives only in SQLite. Notifications use the
existing `/home/ubuntu/telegram_notify.py` helper.

## Runtime

Default database:

`/home/ubuntu/sync_root/db/codex_usage_monitor.db`

Default unit:

`codex-usage-monitor.timer` runs `codex-usage-monitor.service` every 15 minutes.
It uses `/usr/bin/python3` and only global/stdlib Python modules.

## Commands

```bash
python3 codex_usage_monitor.py init-db
python3 codex_usage_monitor.py once
python3 codex_usage_monitor.py status
python3 codex_usage_monitor.py notify-test
```

## Datasette Views

- `latest_state`
- `history`
- `quota_overview`
- `quota_diagnostics`
- `reset_count_changes`
- `recent_failures`

## Fedora Native Codex Session Archive

This repository also contains a passive archive for native Codex CLI sessions.
It does not wrap or replace `codex`; it imports files that Codex already writes
under `~/.codex`.

Archive root:

`/home/daniele/.local/share/codex-session-archive`

Main commands:

```bash
python3 codex_session_archive.py init
python3 codex_session_archive.py import
python3 codex_session_archive.py status
python3 codex_session_archive.py search --prompt-id 4
python3 codex_session_archive.py export --session-id SESSION_ID
python3 codex_session_archive.py verify
python3 codex_session_archive.py install-user-systemd
python3 codex_session_archive.py uninstall-user-systemd
```

The archive stores exact gzip copies of native rollout JSONL files, normalized
JSONL, Markdown views, per-rollout manifests, a SQLite index, and available
Codex history/thread metadata. `archive_id` identifies one materialized rollout
file; `session_id` remains the native Codex thread/session id and may appear on
multiple `archive_id` records when a session was resumed. Normalized files apply
best-effort secret redaction; raw copies are exact and stored with restrictive
permissions.
