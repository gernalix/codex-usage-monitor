# Data model

The quota store is `~/.local/share/codex-usage-monitor/codex_usage_monitor.db`.
Quota observations are append-only. Views include latest_state, history,
quota_overview, quota_diagnostics, reset_count_changes and recent_failures.
Reset-credit metadata is stored with quota snapshots; missing expiry is an
explicit unknown, not an inferred date. Schema ownership stays in the quota
capsule, including its migrations.

The session archive under `~/.local/share/codex-session-archive` contains local
raw gzip copies, normalized JSONL, Markdown, manifests and archive.sqlite.
An archive_id identifies one materialized source; session_id is a native chat
identity and can have multiple archive records. Incremental import compares
source size and stored fingerprints instead of rebuilding unchanged sources.

Derived cost indexes under `index/` include task_costs.sqlite and CSV outputs.
Native token counters are cumulative; prompt costs are derived deltas and do
not prove a still-running prompt's final cost. Native sessions remain the
source; do not replace them with generated indexes.

Raw copies, auth databases, cookies and credentials must not enter Git. Only
redacted derived publication belongs in the private codex-usage repository.
Back up a live SQLite database through its backup API, preserving WAL and
schema; use the owning application for migrations and semantic writes.
