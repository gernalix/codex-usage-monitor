# Complete Codex chat dumps

`codex_chat_dump_publisher.py` runs on the Fedora workstation and mirrors the native Codex rollout JSONL written under `~/.codex/sessions` into the private `gernalix/codex-usage` data repository. It must not run on the Oracle VM because the native Codex session source exists on the workstation.

The existing session archive remains the source for exact local gzip copies. The GitHub mirror is intended for remote inspection without manual copy/paste, so each complete native JSONL record is preserved structurally but passed through the repository's existing redaction logic before publication.

## Layout

Each native Codex session is stored under:

```text
native-sessions/<session-id>/sources/<source-key>/
  manifest.json
  chunks/000001.jsonl
  chunks/000002.jsonl
  ...
```

The repository-wide lookup index is:

```text
index/native-sessions.jsonl
```

A resumed Codex session may have more than one native rollout source. Each source gets its own stable `source-key`; together the ordered chunks contain the complete published dump.

## Incremental behavior

The manifest records the byte offset already published. Each run reads only bytes appended after that offset and publishes only complete JSONL records. A trailing partial record is left for the next run. This avoids repeatedly committing the entire growing chat and keeps Git history growth proportional to new Codex events.

The publisher shares the existing `publisher.lock` and the existing private `codex-usage` checkout. It refuses to commit unrelated dirty paths in that checkout.

## Automation

On Fedora, `codex-usage-publisher.service` runs the normal usage publisher first and then the complete chat-dump publisher. The existing `codex-usage-publisher.timer` runs the service every minute.

`deploy_runtime.py` includes `codex_chat_dump_publisher.py` in the immutable Fedora runtime release.

No duplicate publisher service/timer should remain active on Oracle after the Fedora cutover is verified.

## Manual verification

Run on Fedora:

```bash
python3 codex_chat_dump_publisher.py run --no-push
python3 codex_chat_dump_publisher.py run
systemctl --user status codex-usage-publisher.service
systemctl --user list-timers codex-usage-publisher.timer
```
