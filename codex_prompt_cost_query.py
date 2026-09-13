#!/usr/bin/env python3
"""Query derived Codex PROMPT_ID costs without rebuilding every native rollout."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import codex_task_costs as costs

DEFAULT_DB = Path.home() / ".local/share/codex-session-archive/index/task_costs.sqlite"
DEFAULT_SOURCE_ROOT = Path.home() / ".codex/sessions"


def query_prompt_costs(
    db_path: Path,
    prompt_id: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    latest: bool = False,
    include_incomplete: bool = False,
) -> list[dict[str, Any]]:
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise RuntimeError(f"cost database not found: {db_path}; run codex_task_costs.py to rebuild it")

    where = ["prompt_id = ?"]
    params: list[Any] = [prompt_id]
    if not include_incomplete:
        where.append("COALESCE(completion_state, '') <> 'eof_incomplete'")
    if model is not None:
        where.append("model = ?")
        params.append(model)
    if reasoning_effort is not None:
        where.append("reasoning_effort = ?")
        params.append(reasoning_effort)

    order = "first_timestamp_utc DESC, source_path DESC, prompt_seq DESC" if latest else "first_timestamp_utc, source_path, prompt_seq"
    sql = f"SELECT * FROM prompt_costs WHERE {' AND '.join(where)} ORDER BY {order}"
    if latest:
        sql += " LIMIT 1"

    uri = f"file:{db_path}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as con:
            con.row_factory = sqlite3.Row
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_costs'"
            ).fetchone()
            if exists is None:
                raise RuntimeError("prompt_costs table missing; run codex_task_costs.py to rebuild metrics")
            return [dict(row) for row in con.execute(sql, params).fetchall()]
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read cost database: {exc}") from exc


def find_prompt_rollouts(source_root: Path, prompt_id: str, *, latest_only: bool = False) -> list[Path]:
    """Locate rollouts where prompt_id occurs as a real native user-message boundary.

    For latest_only we still scan only the cheap prompt boundaries across files,
    then choose by the native prompt timestamp. This avoids trusting filesystem
    mtime while ensuring only the winning rollout is fully parsed afterwards.
    """
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"native rollout root not found: {root}")

    matches: list[tuple[str, Path]] = []
    for path in root.rglob("*.jsonl"):
        latest_prompt_ts: str | None = None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                if costs.prompt_id_from_user_message(obj.get("type"), payload.get("type"), payload) == prompt_id:
                    ts = str(obj.get("timestamp") or "")
                    if latest_prompt_ts is None or ts > latest_prompt_ts:
                        latest_prompt_ts = ts
        if latest_prompt_ts is not None:
            matches.append((latest_prompt_ts, path))

    matches.sort(key=lambda item: (item[0], str(item[1])))
    if latest_only and matches:
        return [matches[-1][1]]
    return [path for _ts, path in matches]


def refresh_prompt_costs(
    db_path: Path,
    source_root: Path,
    prompt_id: str,
    *,
    latest_only: bool = False,
) -> dict[str, int]:
    """Refresh only prompt_costs rows for rollout files that really contain prompt_id."""
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise RuntimeError(f"cost database not found: {db_path}; run codex_task_costs.py to rebuild it")
    candidates = find_prompt_rollouts(source_root, prompt_id, latest_only=latest_only)
    if not candidates:
        raise RuntimeError(f"no native rollout contains PROMPT_ID={prompt_id} as a user prompt")

    refreshed: dict[str, list[dict[str, Any]]] = {}
    for path in candidates:
        _session, prompts = costs.analyze_with_prompts(path)
        refreshed[str(path)] = prompts

    try:
        with closing(sqlite3.connect(db_path, timeout=10)) as con:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_costs'"
            ).fetchone()
            if exists is None:
                raise RuntimeError("prompt_costs table missing; run codex_task_costs.py to rebuild metrics")
            columns = {row[1] for row in con.execute("PRAGMA table_info(prompt_costs)")}
            missing = [key for key in costs.PROMPT_KEYS if key not in columns]
            if missing:
                raise RuntimeError(
                    "prompt_costs schema is stale (missing " + ", ".join(missing) + "); run codex_task_costs.py once"
                )
            sql = f"INSERT OR REPLACE INTO prompt_costs ({','.join(costs.PROMPT_KEYS)}) VALUES ({','.join('?' for _ in costs.PROMPT_KEYS)})"
            con.execute("BEGIN")
            for source_path, prompts in refreshed.items():
                con.execute("DELETE FROM prompt_costs WHERE source_path = ?", (source_path,))
                con.executemany(sql, [[row.get(key) for key in costs.PROMPT_KEYS] for row in prompts])
            con.commit()
            con.row_factory = sqlite3.Row
            all_rows = [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM prompt_costs ORDER BY first_timestamp_utc, source_path, prompt_seq"
                ).fetchall()
            ]
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot refresh cost database: {exc}") from exc

    # Keep the convenience CSV synchronized without reparsing unrelated rollouts.
    costs.write_csv(db_path.parent / "prompt_costs.csv", all_rows, costs.PROMPT_KEYS)
    return {
        "rollouts_refreshed": len(refreshed),
        "prompt_rows_refreshed": sum(len(rows) for rows in refreshed.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read per-PROMPT_ID Codex costs from the existing SQLite index; optionally refresh only matching rollouts."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--prompt-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--include-incomplete", action="store_true")
    parser.add_argument(
        "--refresh-prompt",
        action="store_true",
        help="refresh only native rollout files where this PROMPT_ID is a real user prompt before querying",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        if args.refresh_prompt:
            result = refresh_prompt_costs(
                args.db,
                args.source_root,
                args.prompt_id,
                latest_only=args.latest,
            )
            print(
                f"targeted_refresh rollouts={result['rollouts_refreshed']} rows={result['prompt_rows_refreshed']}",
                file=sys.stderr,
            )
        rows = query_prompt_costs(
            args.db,
            args.prompt_id,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            latest=args.latest,
            include_incomplete=args.include_incomplete,
        )
    except RuntimeError as exc:
        parser.exit(2, f"error: {exc}\n")

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
    else:
        for row in rows:
            print(
                f"PROMPT_ID={row['prompt_id']} state={row.get('completion_state')} model={row.get('model')} "
                f"reasoning={row.get('reasoning_effort')} total={row.get('total_tokens')} "
                f"input={row.get('input_tokens')} cached={row.get('cached_input_tokens')} "
                f"uncached={row.get('uncached_input_tokens')} output={row.get('output_tokens')} "
                f"reasoning_tokens={row.get('reasoning_output_tokens')} tools={row.get('tool_call_count')} "
                f"duration={row.get('duration_seconds')}s started={row.get('first_timestamp_utc')}"
            )
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
