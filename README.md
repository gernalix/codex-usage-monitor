# codex-usage-monitor

Independent Oracle VM monitor for Codex quota observations.

The monitor reads the existing Codex app-server JSON-RPC method
`account/rateLimits/read`, stores append-only observations in SQLite, and lets
the existing Datasette service expose the database and views from
`/home/ubuntu/sync_root/db`.

Persistent application state lives only in SQLite. Notifications use the
existing `/home/ubuntu/telegram_notify.py` helper.

> GitHub repository autosync is owned by the separate
> `gernalix/github-autosync` repository. Do not add its script or systemd units
> back to this repository.

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

## Per-session and per-prompt cost metrics

`codex_task_costs.py` derives token and quota-cost metrics directly from native
Codex rollout JSONL. The archive systemd service runs it automatically after
each import. Native `token_count` events are cumulative for the session; the
script additionally derives deltas for each real `PROMPT_ID=` user message, so
multiple roadmap prompts executed in the same Codex chat are measured
separately. `PROMPT_ID` strings merely echoed by tools, files, or assistant
output are not treated as prompt boundaries.

Use the full derivation only when the whole index must be rebuilt:

```bash
python3 codex_task_costs.py
```

For ordinary cost lookups use the read-only SQLite query helper. It does not
rescan `~/.codex/sessions`, rewrite the database, or regenerate CSV files:

```bash
python3 codex_prompt_cost_query.py --prompt-id 284731
python3 codex_prompt_cost_query.py --prompt-id 917364 --reasoning-effort low --latest
python3 codex_prompt_cost_query.py --prompt-id 284731 --json
```

If a just-finished prompt is not indexed yet, use an explicit targeted refresh:

```bash
python3 codex_prompt_cost_query.py --prompt-id 835917 --refresh-prompt --latest --json
```

`--refresh-prompt` first locates only rollout files where that ID appears as a
real native user-prompt boundary, reparses those matching rollout files, and
replaces only their `prompt_costs` rows. It does not rebuild unrelated
`session_costs` or fully parse every rollout. `prompt_costs.csv` is regenerated
from the small SQLite prompt index after that explicit refresh. Use full
`codex_task_costs.py` only for a stale/incompatible schema or an intentional
complete rebuild. A prompt cannot know its own final cost while it is still
running because its final native `token_count` and `task_complete` do not exist
until completion.

Outputs under `~/.local/share/codex-session-archive/index/`:

- `task_costs.sqlite` — `session_costs` plus exact derived `prompt_costs` rows.
- `task_costs.csv` — convenient per-session export.
- `prompt_costs.csv` — per-`PROMPT_ID` token/quota deltas.
- SQLite view `expensive_sessions` — sessions ordered by total tokens.
- SQLite view `expensive_prompts` — prompt executions ordered by total tokens.
- SQLite views `model_reasoning_summary` and `prompt_model_reasoning_summary` — aggregate comparisons by model/reasoning effort.

Captured fields include model, reasoning effort, active duration, tool calls,
reasoning items, input/cached/uncached/output/reasoning/total tokens, cache ratio,
first/last weekly quota percentage and observed quota delta. Per-prompt token
fields are deltas between the cumulative native token counter immediately before
the prompt and the last counter observed before native `task_complete`. The same
`task_complete` event ends `duration_seconds`, so time spent idle before the next
user prompt is excluded. Older rollouts that omit `task_complete` but expose a
following prompt are marked `next_prompt_fallback`; a prompt still active or
abnormally terminated at EOF is marked `eof_incomplete`. Exact queries exclude
`eof_incomplete` rows by default; use `--include-incomplete` only for diagnostics.
This prevents a prompt from reporting its own mid-run token snapshot as a final
cost. Repeated executions of the same `PROMPT_ID` remain separate rows; filters
such as `--model`, `--reasoning-effort` and `--latest` disambiguate them without
inspecting raw rollouts. Quota delta remains an observed prompt/session-window
signal, not an exclusive attribution when concurrent Codex sessions consume the
same quota.

A copied Codex UI/Markdown transcript does **not** include native `token_count`
events, so exact token cost cannot be reconstructed from that transcript alone.
Use the native archive metrics or the diagnostic bundle for exact analysis.

## Diagnostic usage bundle

`codex_session_archive.py` can generate one ChatGPT-uploadable diagnostic ZIP
for token and quota investigations:

```bash
python3 codex_session_archive.py diagnostic-bundle
```

Default output:

`~/.local/share/codex-session-archive/exports/codex-usage-diagnostic-bundle-YYYYMMDDTHHMMSSZ.zip`

The bundle contains `manifest.json`, a README, `task_costs.sqlite` (including the
`prompt_costs` table), `task_costs.csv`, the archive index, redacted normalized
session JSONL, per-session manifests, archive metadata, archive docs and any
valid local quota/rate-limit monitor history. Raw rollout files, raw gzip
archives, native Codex auth/state databases, shell snapshots, locks, temporary
files and old backups are excluded. Missing optional sources are listed in
`manifest.json` instead of failing the command.
