# Retired: curated Markdown vault

The legacy `codex_curated_vault.py` generator and the scheduled
`vault/chats/`, `vault/prompts/`, `vault/reports/`, `vault/bottlenecks/`,
`vault/projects/` Markdown views have been retired. Calling the entrypoint
returns read-only JSON source locators; it never regenerates Markdown.

For analysis use the **native raw JSONL** in
`~/.local/share/codex-session-archive/raw/sessions/`, or the redacted
`normalized/sessions/` JSONL. Locate the correct archived source(s) through
`index/archive.sqlite`, joined to MegaVault `codex_chat_archive_refs` by
`archive_id` and `chat_id`. The original JSONL and archive index, not a
derived transcript or cached `.state.json`, are the sources of truth.

The historical Python parsing/rendering functions are retained solely for
compatibility with existing test fixtures; they are not called by the live
systemd unit or command-line entrypoint.
