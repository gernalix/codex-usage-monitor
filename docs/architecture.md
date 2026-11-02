# Architecture

The Python package is under `src/codex_monitor`. Root scripts are compatibility
adapters; `systemd/` declares each installed entrypoint path (source checkout
or immutable runtime, depending on the unit). `credentials.py`
resolves runtime credentials without owning a second vault.

`capsules/quota` owns quota collection, SQLite snapshots, attribution and
presentation. `session_archive` owns native rollout archival and incremental
indexing. `prompt_analysis` owns cost reconstruction and read-only queries.
`publishing` owns telemetry and redacted chat publication, with a shared lock.
`source_registry` owns source discovery; `runtime_deploy` owns deployment.
The historical orchestration, C2 health and GitHub Actions modules are retired
compatibility paths, not services to reactivate.

Cross-capsule callers use the target public API, not its implementation. The
AST check in `scripts/check_architecture_boundaries.py` checks private imports,
adapter logic, wildcard imports and namespace monkey-patching. The existing
`codex_human_log.py` is a separate passive readable-log entrypoint.

Fedora System Monitor owns host health; github-autosync owns repository
synchronization; `codex-usage` is the separate private published-data repo.
See README.md for current topology. Documentation is not live runtime proof.
