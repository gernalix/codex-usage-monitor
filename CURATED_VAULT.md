# Curated Codex Vault

`codex_curated_vault.py` derives a compact Markdown knowledge vault from `normalized/sessions/*.jsonl` after the native archive import.

It does not replace the raw/normalized archive. The native archive remains the source of truth; this vault is a navigable derived view intended for Daniele and future AI analysis.

Default output:

`~/.local/share/codex-session-archive/vault`

Structure:

- `index.md`: bullet indexes for chats and prompts.
- `chats/<session_id>.md`: cleaned session view.
- `prompts/<PROMPT_ID>.md`: prompt-centric view.
- `projects/<project_id>.md`: project index.
- `reports/<session_id>-<event_index>.md`: final reports identified structurally from `task_complete`/`turn_complete`.
- `bottlenecks/<session_id>-<event_index>.md`: evidence that may indicate friction worth investigating separately.
- `.state.json`: incremental fingerprints and extracted metadata; not intended for manual reading.

The generated notes contain explicit Obsidian-style wiki links in both directions between chat, prompt, project, final report and bottleneck notes where the relationship is known.

Timestamps are preserved and rendered in `Europe/Copenhagen` with UTC offset.

The curated vault keeps user prompts, visible assistant progress, concise tool/action invocations, final reports and bottleneck evidence. It intentionally omits injected `AGENTS.md` context and routine low-value event noise such as token counters and item-completed markers.

Bottleneck detection is deliberately signal-oriented rather than diagnostic. It records non-zero tool exits, exposed reasoning-summary text that contains friction/error terms, and visible assistant progress that mentions failures, retries, missing state, timeouts, conflicts, locks, rate limits, workarounds or similar friction. Final reports are not themselves classified as progress bottlenecks. Each bottleneck note says that the signal should be investigated separately if recurrent, costly or blocking.

Private chain-of-thought is not available from Codex and is not reconstructed. If Codex exposes a reasoning summary as readable text, only that exposed text can participate in bottleneck detection.

The generator is incremental: unchanged normalized session files are reused from `.state.json`; only new or changed normalized files are reparsed. `--rebuild` forces a full regeneration.

Manual commands:

```bash
python3 codex_curated_vault.py
python3 codex_curated_vault.py --rebuild
python3 codex_curated_vault.py --archive-root ~/.local/share/codex-session-archive
```

The repository systemd unit runs the curated-vault generator after a successful native archive import.
