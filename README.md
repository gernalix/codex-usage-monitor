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
- `reset_count_changes`
- `recent_failures`
