#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any

import codex_usage_publisher_legacy as legacy
from codex_usage_publisher_legacy import *  # noqa: F401,F403


VERSION = "2026.09.07"
_GOAL_PREFIX = '<codex_internal_context source="goal">'
_legacy_connect_state = legacy.connect_state
_legacy_parse_session = legacy.parse_session
_legacy_export_repo = legacy.export_repo
_legacy_send_batch_telegram = legacy.send_batch_telegram
_legacy_command_run = legacy.command_run
_should_export = False
_notify_cycle_objects: set[int] = set()


def prompt_id_from_text(text: str) -> str | None:
    prompt_id = legacy.archive.prompt_id_from_text(text)
    if prompt_id:
        return prompt_id
    normalized = (text or "").replace(r"\_", "_")
    match = re.search(r"\bPROMPT_ID\s*[:=]\s*[`*_~]*([A-Za-z0-9_.-]+)\b", normalized)
    return match.group(1) if match else None


def connect_state(state_dir: Path) -> sqlite3.Connection:
    con = _legacy_connect_state(state_dir)
    columns = {str(row["name"]) for row in con.execute("PRAGMA table_info(cycles)")}
    if "cycle_sha256" not in columns:
        con.execute("ALTER TABLE cycles ADD COLUMN cycle_sha256 TEXT")
        con.commit()
    return con


def _fingerprint(cycle: dict[str, Any]) -> str:
    payload = {"metrics": cycle["metrics"], "events": cycle["events"]}
    return legacy.digest_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def parse_session(path: Path, con: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    global _should_export
    cycles, events = _legacy_parse_session(path, con)
    last_prompt_id: str | None = None
    migrated = False

    for cycle in cycles:
        metrics = cycle["metrics"]
        prompt_id = metrics.get("prompt_id")
        prompt_text = str(metrics.get("prompt_text_redacted") or "")
        final_text = str(metrics.get("final_response_redacted") or "")
        final_prompt_id = prompt_id_from_text(final_text)

        if not prompt_id:
            if final_prompt_id:
                prompt_id = final_prompt_id
            elif prompt_text.lstrip().startswith(_GOAL_PREFIX) and last_prompt_id:
                prompt_id = last_prompt_id
            if prompt_id:
                metrics["prompt_id"] = prompt_id
        if prompt_id:
            last_prompt_id = str(prompt_id)

        fingerprint = _fingerprint(cycle)
        cycle["source_sha256"] = fingerprint
        cycle["cycle_sha256"] = fingerprint
        key = str(metrics["cycle_key"])
        row = con.execute(
            "SELECT source_sha256,cycle_sha256,published_commit,prompt_id,telegram_sent FROM cycles WHERE cycle_key=?",
            (key,),
        ).fetchone()

        # One-time migration: old rows stored the SHA of the entire growing rollout.
        # Seed the stable per-cycle fingerprint without making every old cycle pending once.
        if row and row["published_commit"] and row["cycle_sha256"] is None and row["prompt_id"] == metrics.get("prompt_id"):
            con.execute(
                "UPDATE cycles SET source_sha256=?,cycle_sha256=?,updated_at_utc=? WHERE cycle_key=?",
                (fingerprint, fingerprint, legacy.utc_stamp(), key),
            )
            migrated = True
            source_sha = fingerprint
            cycle_sha = fingerprint
        else:
            source_sha = row["source_sha256"] if row else None
            cycle_sha = row["cycle_sha256"] if row else None

        is_new = row is None or row["published_commit"] is None
        content_changed = row is not None and cycle_sha is not None and source_sha != fingerprint
        prompt_changed = row is not None and row["prompt_id"] != metrics.get("prompt_id")
        if is_new or content_changed or prompt_changed:
            _should_export = True
        if is_new or (row is not None and row["published_commit"] and not row["telegram_sent"]):
            _notify_cycle_objects.add(id(cycle))

    if migrated:
        con.commit()
    return cycles, events


def export_repo(repo: Path, cycles: list[dict[str, Any]], chat_events_by_id: dict[int, list[dict[str, Any]]], quota_db: Path) -> None:
    # Do not create a Git commit just because an unfinished rollout appended events.
    if not _should_export:
        return
    _legacy_export_repo(repo, cycles, chat_events_by_id, quota_db)


def send_batch_telegram(cycles: list[dict[str, Any]], dry_run: bool) -> bool:
    selected = [cycle for cycle in cycles if id(cycle) in _notify_cycle_objects]
    if not selected:
        return True
    return _legacy_send_batch_telegram(selected, dry_run)


def _install_runtime() -> None:
    legacy.VERSION = VERSION
    legacy.connect_state = connect_state
    legacy.prompt_id_from_text = prompt_id_from_text
    legacy.parse_session = parse_session
    legacy.export_repo = export_repo
    legacy.send_batch_telegram = globals()["send_batch_telegram"]
    # Keep the original publisher tests/mock hooks working through this compatibility layer.
    legacy.assert_private_repo = globals()["assert_private_repo"]
    legacy.ensure_repo = globals()["ensure_repo"]
    legacy.git_ok = globals()["git_ok"]
    legacy.run = globals()["run"]
    legacy.command_run = command_run


def command_run(args) -> int:
    global _should_export, _notify_cycle_objects
    _should_export = False
    _notify_cycle_objects = set()
    _install_runtime()
    return int(_legacy_command_run(args))


def main(argv: list[str] | None = None) -> int:
    _install_runtime()
    return int(legacy.main(argv))


_install_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
