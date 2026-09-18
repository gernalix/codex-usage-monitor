# codex-usage-monitor

Fedora-local monitor, archive and publisher for Codex usage data.

## Canonical runtime topology

The **only canonical runtime for this repository is the Fedora workstation where Codex runs**. This is intentional: quota acquisition talks to the local Codex app-server, session/archive tooling reads `~/.codex`, and the usage/chat publishers read native rollouts from `~/.codex/sessions`.

The Oracle VM is **not** a runtime for `codex-usage-monitor` anymore. It may continue to host genuinely always-on infrastructure such as Uptime Kuma or Datasette, but it must not run a second Codex quota collector/publisher and does not need a working clone of this repository after the migration is verified.

GitHub repository autosync remains owned by the separate `gernalix/github-autosync` repository. Do not add its script or systemd units here.

### Fedora paths

Quota database:

`/home/daniele/.local/share/codex-usage-monitor/codex_usage_monitor.db`

State:

`/home/daniele/.local/state/codex-usage-monitor`

Source checkout:

`/home/daniele/projects/codex-usage-monitor`

Private published data checkout:

`/home/daniele/projects/codex-usage`

Native Codex sessions:

`/home/daniele/.codex/sessions`

### Fedora services

- `codex-usage-monitor.timer` → quota acquisition every 15 minutes.
- `codex-session-archive.timer` → passive local archive/index updates.
- `codex-usage-publisher.timer` → usage/prompt/chat publication and complete redacted native chat dumps.

There must be no equivalent active `codex-usage-monitor` service/timer on the Oracle VM after the Fedora cutover is validated.

## Quota monitor

The monitor reads the Codex app-server JSON-RPC method `account/rateLimits/read`, stores append-only observations in SQLite, and can send deduplicated Telegram notifications.

Commands:

```bash
python3 codex_usage_monitor.py init-db
python3 codex_usage_monitor.py once
python3 codex_usage_monitor.py status
python3 codex_usage_monitor.py notify-test
```

SQLite views include:

- `latest_state`
- `history`
- `quota_overview`
- `quota_diagnostics`
- `reset_count_changes`
- `recent_failures`

A successful Fedora acquisition can also send a best-effort Uptime Kuma Push heartbeat. Kuma may live on the Oracle VM; the collector itself remains local on Fedora. See `UPTIME_KUMA.md`.

## Fedora native Codex session archive

The passive archive imports files Codex already writes under `~/.codex`; it does not wrap or replace Codex.

Archive root:

`/home/daniele/.local/share/codex-session-archive`

Commands:

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

The archive stores exact gzip copies of native rollout JSONL files, normalized JSONL, Markdown views, per-rollout manifests, a SQLite index and available Codex history/thread metadata. `archive_id` identifies one materialized rollout file; `session_id` remains the native Codex thread/session id and can appear on multiple archive records when a session is resumed.

Normalized files apply best-effort secret redaction. Exact raw copies remain local with restrictive permissions and are not published to GitHub.

The timer hot path is incremental. `codex_session_archive_incremental.py` compares current rollout size with `source_size_bytes` already stored in `archive.sqlite`; unchanged append-only files are not reopened, copied or hashed. The full `codex_session_archive.py import` remains the explicit fallback/rebuild path.

## Per-session and per-prompt cost metrics

`codex_task_costs.py` is the canonical full rebuild from native Codex rollout JSONL. Native `token_count` events are cumulative for a session; derived `prompt_costs` rows are deltas for real `PROMPT_ID=` user messages, so multiple roadmap prompts executed in one Codex chat remain separate measurements.

`codex_task_costs_incremental.py` uses archive fingerprints and reparses only new or changed rollout files. For ordinary lookups use the read-only helper:

```bash
python3 codex_prompt_cost_query.py --prompt-id 284731
python3 codex_prompt_cost_query.py --prompt-id 917364 --reasoning-effort low --latest
python3 codex_prompt_cost_query.py --prompt-id 284731 --json
```

If a lookup happens before the timer has processed a just-finished prompt, use a targeted refresh:

```bash
python3 codex_prompt_cost_query.py --prompt-id 835917 --refresh-prompt --latest --json
```

Outputs under `~/.local/share/codex-session-archive/index/` include:

- `task_costs.sqlite`
- `task_costs.csv`
- `prompt_costs.csv`
- `expensive_sessions`
- `expensive_prompts`
- `model_reasoning_summary`
- `prompt_model_reasoning_summary`

Captured fields include model, reasoning effort, active duration, tool calls, reasoning items, input/cached/uncached/output/reasoning/total tokens, cache ratio and observed weekly quota changes. A prompt cannot know its final cost while it is still running because final `token_count` and `task_complete` events do not exist yet.

## Usage publisher

`codex_usage_publisher.py` publishes redacted per-prompt and per-chat usage artifacts to the private `gernalix/codex-usage` repository. It also contains the completion guard used to detect local repositories that were modified but not clean/synchronized when a Codex task finished.

The publisher is a Fedora-local process because it consumes native Codex rollouts and local repository state.

Publisher invocations share one filesystem lock. `run` now waits up to 30 seconds by default for a concurrent timer/manual invocation to finish instead of immediately returning `status=locked`; use `run --wait-lock-seconds 0` only when an immediate non-waiting probe is explicitly desired. This keeps runtime validation from failing just because the periodic timer happened to start at the same moment.

## Complete redacted chat dumps

`codex_chat_dump_publisher.py` incrementally mirrors native Codex rollout records to the private `gernalix/codex-usage` repository for remote inspection without manual copy/paste.

Published layout:

```text
native-sessions/<session-id>/sources/<source-key>/
  manifest.json
  chunks/000001.jsonl
  chunks/000002.jsonl
  ...
```

Global lookup index:

```text
index/native-sessions.jsonl
```

The GitHub copy is structurally complete for readable native records but secret-redacted. Exact raw rollouts remain only in the local archive. See `CHAT_DUMPS.md`.

## Diagnostic usage bundle

Generate a ChatGPT-uploadable diagnostic bundle with:

```bash
python3 codex_session_archive.py diagnostic-bundle
```

Default output:

`~/.local/share/codex-session-archive/exports/codex-usage-diagnostic-bundle-YYYYMMDDTHHMMSSZ.zip`

The bundle contains the usage databases/CSVs, redacted normalized session JSONL, per-session manifests and archive metadata. Raw rollouts, raw gzip archives, native Codex auth/state databases, shell snapshots, locks and secrets are excluded.

## Repository verification

Use the repository verification helper for local and Codex checks; it injects the src-layout path and keeps targeted unittest runs reproducible:

```bash
python3 scripts/verify_repo.py tests.test_publishing_status tests.test_publication_semantic_backfill
```

Targeted `python3 -m unittest tests.<module>` runs from the repository root are also source-layout safe. When verification and deployment share one shell call, use fail-fast shell semantics (`set -euo pipefail` or `&&`) so a failed test can never fall through to deployment.

## Deployment rule

Production Fedora runtime must be deployed from a **clean, upstream-synchronized** `main` using `deploy_runtime.py`. The immutable runtime lives under:

`/home/daniele/.local/lib/codex-usage-monitor`

Do not maintain a second production deployment on the Oracle VM. If the VM still contains historical service/timer units or a checkout of this repository, retire them only after Fedora acquisition, archive, publisher, chat-dump publication and heartbeat behavior have all been verified.