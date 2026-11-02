# codex-usage-monitor

Fedora owns the runtime; Oracle must not run a duplicate collector. Read
`project-capsule.yaml` for commands, `docs/architecture.md` for boundaries,
`docs/operations.md` for runtime operations and `docs/data-model.md` for data.

Keep the public `api.py` boundaries between `src/codex_monitor/capsules/`
modules and keep compatibility scripts thin. Reuse `scripts/verify_repo.py`.
Use isolated task worktrees and the installed Git single writer.

Preserve native rollouts, local archives, databases and published telemetry.
Never publish raw sessions or credential values. Credential changes use the
Bitwarden runtime defined by the current shared manifest; preserve existing
specialist fallbacks until consumer migration is verified.

Do not run collectors, publishers, notification tests, deployers or installers
as verification. Historical C2/C3 entrypoints remain retired. No notifications
or service changes are implied by a source/documentation change.
