#!/usr/bin/env python3
"""Incrementally import append-only native Codex rollouts without hashing unchanged files."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
from typing import Any

from . import api as archive

DEFAULT_ARCHIVE_ROOT = archive.DEFAULT_ARCHIVE_ROOT
DEFAULT_SOURCE_ROOT = archive.DEFAULT_SOURCE_ROOT
DEFAULT_CODEX_DIR = archive.DEFAULT_CODEX_DIR


def archived_session_sizes(root: Path) -> dict[str, int]:
    archive.init_db(root)
    with closing(archive.connect_db(root)) as con:
        rows = con.execute("SELECT source_path,source_size_bytes FROM sessions").fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def archived_source_sizes(root: Path) -> dict[str, int]:
    archive.init_db(root)
    with closing(archive.connect_db(root)) as con:
        rows = con.execute("SELECT source_path,source_size_bytes FROM source_files").fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def changed_rollouts(source_root: Path, known_sizes: dict[str, int]) -> tuple[list[Path], int]:
    changed: list[Path] = []
    seen = 0
    for path in sorted(source_root.glob("**/*.jsonl")):
        if not path.is_file():
            continue
        seen += 1
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if known_sizes.get(str(path)) != size:
            changed.append(path)
    return changed, seen


def source_file_changed(path: Path, known_sizes: dict[str, int]) -> bool:
    if not path.is_file():
        return False
    try:
        return known_sizes.get(str(path)) != path.stat().st_size
    except FileNotFoundError:
        return False


def import_changed_shell_snapshots(root: Path, codex_dir: Path, known_sizes: dict[str, int]) -> int:
    snap_dir = codex_dir / "shell_snapshots"
    if not snap_dir.is_dir():
        return 0
    imported = 0
    for path in sorted(snap_dir.glob("*.sh")):
        if not source_file_changed(path, known_sizes):
            continue
        session_id = path.name.split(".", 1)[0]
        if archive.import_source_file(
            root,
            path,
            "shell_snapshot",
            Path("raw/shell_snapshots") / session_id / f"{path.name}.gz",
        ):
            imported += 1
    return imported


def incremental_import(root: Path, source_root: Path, codex_dir: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    codex_dir = codex_dir.expanduser().resolve()
    archive.init_db(root)
    archive.ensure_private_dir(root)

    # Read the archive state only after taking the import lock. Otherwise a
    # concurrent full/incremental import can update the index between the
    # preflight and lock acquisition, making this run operate on stale sizes.
    with archive.ExclusiveLock(root / "locks/import.lock", blocking=True):
        known_sessions = archived_session_sizes(root)
        known_sources = archived_source_sizes(root)
        changed, seen = changed_rollouts(source_root, known_sessions)

        run_started = archive.utc_stamp()
        with closing(archive.connect_db(root)) as con:
            cur = con.execute(
                "INSERT INTO import_runs (started_at_utc,status) VALUES (?,?)",
                (run_started, "running"),
            )
            run_id = int(cur.lastrowid)
            con.commit()

        imported = 0
        source_files = 0
        error: str | None = None
        try:
            for path in changed:
                _archive_id, did_import, _manifest = archive.import_session(root, source_root, path)
                if did_import:
                    imported += 1

            history = codex_dir / "history.jsonl"
            if source_file_changed(history, known_sources):
                if archive.import_source_file(root, history, "history", Path("raw/history/history.jsonl.gz")):
                    source_files += 1

            # Thread metadata is a derived view over Codex state and remains cheap
            # compared with hashing every rollout. Preserve the existing export.
            if archive.export_threads_metadata(root, codex_dir):
                source_files += 1
            source_files += import_changed_shell_snapshots(root, codex_dir, known_sources)
            status = "ok"
        except Exception as exc:  # mirror the archive command's failure accounting
            status = "error"
            error = archive.redact_text(str(exc))

        with closing(archive.connect_db(root)) as con:
            con.execute(
                """
                UPDATE import_runs
                SET completed_at_utc=?, status=?, sessions_seen=?, sessions_imported=?,
                    sessions_skipped=?, source_files_imported=?, error=?
                WHERE run_id=?
                """,
                (
                    archive.utc_stamp(),
                    status,
                    seen,
                    imported,
                    max(0, seen - imported),
                    source_files,
                    error,
                    run_id,
                ),
            )
            con.commit()

    archive.enforce_private_tree(root)
    return {
        "status": status,
        "mode": "incremental",
        "sessions_seen": seen,
        "sessions_candidates": len(changed),
        "sessions_imported": imported,
        "sessions_skipped": max(0, seen - imported),
        "source_files_imported": source_files,
        "archive_root": str(root),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import only new/grown append-only native Codex rollouts, using archive source sizes as a preflight."
    )
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--codex-dir", type=Path, default=DEFAULT_CODEX_DIR)
    args = parser.parse_args()
    result = incremental_import(args.archive_root, args.source_root, args.codex_dir)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
