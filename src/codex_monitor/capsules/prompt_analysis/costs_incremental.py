#!/usr/bin/env python3
"""Incrementally update Codex cost metrics from rollouts changed by the archive importer."""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from . import costs_api as costs

DEFAULT_ARCHIVE_ROOT = Path.home() / ".local/share/codex-session-archive"


def archive_sources(archive_root: Path) -> dict[str, tuple[str, int]]:
    archive_db = archive_root / "index/archive.sqlite"
    if not archive_db.is_file():
        raise RuntimeError(f"archive index not found: {archive_db}")
    uri = f"file:{archive_db.resolve()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            if exists is None:
                raise RuntimeError("archive sessions table missing")
            rows = con.execute(
                "SELECT source_path,source_sha256,source_size_bytes FROM sessions ORDER BY source_path"
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read archive index: {exc}") from exc
    return {str(path): (str(sha), int(size)) for path, sha, size in rows}


def ensure_state_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_source_state (
            source_path TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            source_size_bytes INTEGER NOT NULL
        )
        """
    )


def cost_schema_ready(con: sqlite3.Connection) -> bool:
    names = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('session_costs','prompt_costs')"
        )
    }
    return names == {"session_costs", "prompt_costs"}


def atomic_write_csv(path: Path, rows: list[dict[str, Any]], keys: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows([{key: row.get(key) for key in keys} for row in rows])
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def snapshot_rows(db_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
        con.row_factory = sqlite3.Row
        sessions = [dict(row) for row in con.execute("SELECT * FROM session_costs ORDER BY first_timestamp_utc,source_path")]
        prompts = [dict(row) for row in con.execute("SELECT * FROM prompt_costs ORDER BY first_timestamp_utc,source_path,prompt_seq")]
    return sessions, prompts


def bootstrap(archive_root: Path, sources: dict[str, tuple[str, int]]) -> dict[str, Any]:
    session_rows: list[dict[str, Any]] = []
    prompt_rows: list[dict[str, Any]] = []
    valid_sources: dict[str, tuple[str, int]] = {}
    for source_path, fingerprint in sources.items():
        path = Path(source_path)
        if not path.is_file():
            continue
        session, prompts = costs.analyze_with_prompts(path)
        session_rows.append(session)
        prompt_rows.extend(prompts)
        valid_sources[source_path] = fingerprint

    db_path = archive_root / "index/task_costs.sqlite"
    if db_path.exists():
        with closing(sqlite3.connect(db_path, timeout=10)) as con:
            if cost_schema_ready(con):
                con.execute("DELETE FROM session_costs")
                con.execute("DELETE FROM prompt_costs")
                con.commit()
    costs.write_outputs(session_rows, prompt_rows, archive_root)
    with closing(sqlite3.connect(db_path, timeout=10)) as con:
        ensure_state_schema(con)
        con.execute("DELETE FROM cost_source_state")
        con.executemany(
            "INSERT INTO cost_source_state(source_path,source_sha256,source_size_bytes) VALUES (?,?,?)",
            [(path, sha, size) for path, (sha, size) in valid_sources.items()],
        )
        con.commit()
    return {
        "mode": "bootstrap",
        "sources_seen": len(sources),
        "sources_parsed": len(valid_sources),
        "sources_removed": 0,
    }


def incremental_update(archive_root: Path) -> dict[str, Any]:
    archive_root = archive_root.expanduser().resolve()
    sources = archive_sources(archive_root)
    db_path = archive_root / "index/task_costs.sqlite"
    if not db_path.is_file():
        return bootstrap(archive_root, sources)

    with closing(sqlite3.connect(db_path, timeout=10)) as con:
        if not cost_schema_ready(con):
            return bootstrap(archive_root, sources)
        ensure_state_schema(con)
        state_rows = con.execute(
            "SELECT source_path,source_sha256,source_size_bytes FROM cost_source_state"
        ).fetchall()
        state = {str(path): (str(sha), int(size)) for path, sha, size in state_rows}
    if not state:
        return bootstrap(archive_root, sources)

    changed = [path for path, fingerprint in sources.items() if state.get(path) != fingerprint]
    removed = sorted(set(state) - set(sources))
    parsed: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    fingerprints: dict[str, tuple[str, int]] = {}
    for source_path in changed:
        path = Path(source_path)
        if not path.is_file():
            continue
        parsed[source_path] = costs.analyze_with_prompts(path)
        fingerprints[source_path] = sources[source_path]

    if not parsed and not removed:
        return {
            "mode": "incremental",
            "sources_seen": len(sources),
            "sources_parsed": 0,
            "sources_removed": 0,
        }

    session_sql = f"INSERT OR REPLACE INTO session_costs ({','.join(costs.SESSION_KEYS)}) VALUES ({','.join('?' for _ in costs.SESSION_KEYS)})"
    prompt_sql = f"INSERT OR REPLACE INTO prompt_costs ({','.join(costs.PROMPT_KEYS)}) VALUES ({','.join('?' for _ in costs.PROMPT_KEYS)})"
    try:
        with closing(sqlite3.connect(db_path, timeout=20)) as con:
            ensure_state_schema(con)
            con.execute("BEGIN IMMEDIATE")
            for source_path in removed:
                con.execute("DELETE FROM prompt_costs WHERE source_path=?", (source_path,))
                con.execute("DELETE FROM session_costs WHERE source_path=?", (source_path,))
                con.execute("DELETE FROM cost_source_state WHERE source_path=?", (source_path,))
            for source_path, (session, prompts) in parsed.items():
                con.execute("DELETE FROM prompt_costs WHERE source_path=?", (source_path,))
                con.execute("DELETE FROM session_costs WHERE source_path=?", (source_path,))
                con.execute(session_sql, [session.get(key) for key in costs.SESSION_KEYS])
                con.executemany(prompt_sql, [[row.get(key) for key in costs.PROMPT_KEYS] for row in prompts])
                sha, size = fingerprints[source_path]
                con.execute(
                    """
                    INSERT INTO cost_source_state(source_path,source_sha256,source_size_bytes)
                    VALUES (?,?,?)
                    ON CONFLICT(source_path) DO UPDATE SET
                        source_sha256=excluded.source_sha256,
                        source_size_bytes=excluded.source_size_bytes
                    """,
                    (source_path, sha, size),
                )
            con.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot update cost index: {exc}") from exc

    sessions, prompts = snapshot_rows(db_path)
    atomic_write_csv(archive_root / "index/task_costs.csv", sessions, costs.SESSION_KEYS)
    atomic_write_csv(archive_root / "index/prompt_costs.csv", prompts, costs.PROMPT_KEYS)
    return {
        "mode": "incremental",
        "sources_seen": len(sources),
        "sources_parsed": len(parsed),
        "sources_removed": len(removed),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Incrementally update Codex token-cost indexes from the archive source index.")
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    args = parser.parse_args()
    try:
        result = incremental_update(args.archive_root)
    except RuntimeError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
