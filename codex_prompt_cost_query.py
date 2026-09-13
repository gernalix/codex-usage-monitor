#!/usr/bin/env python3
"""Query already-derived Codex PROMPT_ID costs without rescanning native rollouts."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any

DEFAULT_DB = Path.home() / ".local/share/codex-session-archive/index/task_costs.sqlite"


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read per-PROMPT_ID Codex cost metrics from the existing SQLite index without rebuilding it."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--prompt-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--include-incomplete", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
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
