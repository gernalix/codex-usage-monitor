# codex-usage-monitor

Unified monitor for Codex quota observations and native Codex session archives.

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
python3 codex_usage_monitor.py usage init-db
python3 codex_usage_monitor.py usage once
python3 codex_usage_monitor.py usage status
python3 codex_usage_monitor.py usage notify-test
```

The historical `python3 codex_usage_monitor.py once` style is still supported
for compatibility.

## Datasette Views

- `latest_state`
- `history`
- `quota_overview`
- `quota_diagnostics`
- `reset_count_changes`
- `recent_failures`

## Fedora Native Codex Session Archive

The same application contains a passive archive for native Codex CLI sessions.
It does not wrap or replace `codex`; it imports files that Codex already writes
under `~/.codex`.

Archive root:

`/home/daniele/.local/share/codex-session-archive`

Main commands:

```bash
python3 codex_usage_monitor.py sessions init
python3 codex_usage_monitor.py sessions import
python3 codex_usage_monitor.py sessions status
python3 codex_usage_monitor.py sessions search --prompt-id 4
python3 codex_usage_monitor.py sessions export --session-id SESSION_ID
python3 codex_usage_monitor.py sessions validate-export EXPORT.tar.gz
python3 codex_usage_monitor.py sessions verify
python3 codex_usage_monitor.py sessions verify --deep
python3 codex_usage_monitor.py sessions install-user-systemd
python3 codex_usage_monitor.py sessions uninstall-user-systemd
```

Top-level shortcuts are also available:

```bash
python3 codex_usage_monitor.py search --prompt-id 4
python3 codex_usage_monitor.py export --prompt-id 4
python3 codex_usage_monitor.py validate-export EXPORT.tar.gz
python3 codex_usage_monitor.py verify --deep
```

The historical `codex_session_archive.py` wrapper is kept for compatibility and
delegates to the package implementation.

The archive stores exact gzip copies of native rollout JSONL files, normalized
JSONL, Markdown views, per-rollout manifests, a SQLite index, and available
Codex history/thread metadata. `archive_id` identifies one materialized rollout
file; `session_id` remains the native Codex thread/session id and may appear on
multiple `archive_id` records when a session was resumed.

Session status separates archive records from unique `session_id` values and
distinguishes `complete`, `incomplete`, `partial`, and `corrupt`. A final
truncated JSONL tail is `partial_tail`, not permanent corruption. Internal
invalid JSON is `invalid_internal_json`.

`PROMPT_ID` search uses relational tables (`prompts`, `session_prompts`). The
legacy CSV field remains for existing consumers, and migration is automatic and
idempotent.

`verify` checks raw archive hash, raw payload hash, normalized/Markdown/manifest
hashes, JSON parsing, event counts and event indexes, DB consistency, and
manifest consistency. `verify --deep` regenerates normalized events from raw
gzip archives and compares them semantically. Exports contain
`EXPORT_MANIFEST.json` with per-member hashes and can be checked with
`validate-export`.

Repository branch/SHA stored during session import is the repository state
observed from the session `cwd` at import time. It is not asserted to be the
state at the original Codex session time.

Normalized files apply best-effort secret redaction; raw copies are exact and
stored with restrictive permissions. Import rejects symlink session sources.
