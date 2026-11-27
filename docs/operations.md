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

On Fedora the publisher can read cleaned event text from a previously exported
chat transcript. Its private SQLite state records the native file SHA-256, chat
ID, byte count, export SHA-256 and a fingerprint covering the Python runtime and
publisher rules. It records a receipt only after independently rendering the
events and verifying the actual export bytes. A change to the input, rules or
transcript requires fresh redaction. Sessions whose whole-fragment masking
changes an event also use fresh redaction. Paths, prompt IDs and completion
metrics are derived during each parse. The journal fields `transcripts_checked`
and `transcripts_reused` count native sources during a productive scan.

The publisher compares the resulting bytes with each private regular output file before
replacing it. Identical files retain their inode and modification time. Each
assigned prompt's flat alias contains the latest cycle. Changed files retain
atomic replacement and the regular publication hook.

The usage and chat publishers keep an interrupted push in
`push-retry.json` under their existing state directory and share their existing lock.
The next eligible invocation retries the committed data before exporting another
snapshot. A normal Git push, including the pre-push secret check, has up to 20
minutes. On failure the delay grows from five minutes to a maximum of six hours.
Inspect the user unit journal for the reason if the backlog persists. Source
files, local commits and the remote's normal fast-forward protection are preserved.

When the installed Gitleaks hook rejects a specific outgoing tip for a possible
credential, the publisher records its object ID in the private retry receipt.
Subsequent timer ticks leave that history in quarantine, including after local
commits are appended. Rebuild the rejected unpublished history under the publisher
lock after preserving a private backup. The replacement goes through the normal
private-remote and pre-push checks. The timer reports the hold without rescanning.
