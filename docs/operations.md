# Operations

Run `python3 scripts/verify_repo.py` from the repository root for architecture
checks, unit tests and diff hygiene. Supply unittest module names for targeted
checks. Tests use the source tree through the helper's PYTHONPATH. No venv or
application build is required; runtime uses the global Python installation.

Fedora scheduling definitions are in `systemd/`: quota acquisition, session
archive, telemetry publisher and human-readable log. Their checked-in unit
files describe intended configuration, not current service health. Read-only
status can be inspected separately with systemctl; do not trigger a collector
or publisher to validate documentation. Publishers can write Git and archives;
notification tests can contact Telegram.

Deployment is a separate operation through `deploy_runtime.py`, only from a
clean upstream-synchronized main. The immutable installed runtime is under
`~/.local/lib/codex-usage-monitor`; changing source docs does not reinstall it.
Do not create a second Oracle runtime. Preserve the previous installed runtime
and private SQLite backups before a runtime migration.

Credential consumers and update modes are tracked in the shared Bitwarden
runtime manifest. A running process retains its startup credentials; a file
prepared for a later restart is not evidence of a completed consumer cutover.
Native rollouts and raw archive copies stay private and local.
