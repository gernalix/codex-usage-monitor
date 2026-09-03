#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

DEFAULT_SOURCE_ROOT = Path.home() / ".codex/sessions"
DEFAULT_ARCHIVE_ROOT = Path.home() / ".local/share/codex-session-archive"
PROMPT_RE = re.compile(r"\bPROMPT_ID\s*[:=]\s*([A-Za-z0-9_.-]+)\b")


def parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def intv(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def analyze(path: Path) -> dict[str, Any]:
    session_id = path.stem
    first_ts = last_ts = None
    model = reasoning_effort = cwd = None
    prompt_ids: set[str] = set()
    tool_calls = reasoning_items = turns = 0
    latest_usage: dict[str, Any] = {}
    first_quota = last_quota = None
    reset_at = None

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            ts = parse_ts(obj.get("timestamp"))
            if ts and first_ts is None:
                first_ts = ts
            if ts:
                last_ts = ts
            top = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            ptype = payload.get("type")

            if top == "session_meta":
                session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                cwd = payload.get("cwd") or cwd
            elif top == "turn_context":
                turns += 1
                model = payload.get("model") or model
                cwd = payload.get("cwd") or cwd
                collab = payload.get("collaboration_mode")
                if isinstance(collab, dict):
                    settings = collab.get("settings")
                    if isinstance(settings, dict):
                        reasoning_effort = settings.get("reasoning_effort") or reasoning_effort
                        model = settings.get("model") or model

            if ptype == "function_call":
                tool_calls += 1
            if ptype == "reasoning":
                reasoning_items += 1

            text = json.dumps(payload, ensure_ascii=False)
            for match in PROMPT_RE.finditer(text):
                prompt_ids.add(match.group(1))

            if top == "event_msg" and ptype == "token_count":
                info = payload.get("info")
                if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                    latest_usage = info["total_token_usage"]
                limits = payload.get("rate_limits")
                if isinstance(limits, dict):
                    primary = limits.get("primary")
                    if isinstance(primary, dict) and primary.get("used_percent") is not None:
                        q = float(primary["used_percent"])
                        if first_quota is None:
                            first_quota = q
                        last_quota = q
                        reset_at = primary.get("resets_at") or reset_at

    duration = (last_ts - first_ts).total_seconds() if first_ts and last_ts else None
    input_tokens = intv(latest_usage.get("input_tokens"))
    cached = intv(latest_usage.get("cached_input_tokens"))
    output = intv(latest_usage.get("output_tokens"))
    reasoning = intv(latest_usage.get("reasoning_output_tokens"))
    total = intv(latest_usage.get("total_tokens"))
    return {
        "session_id": session_id,
        "source_path": str(path),
        "first_timestamp_utc": first_ts.isoformat().replace("+00:00", "Z") if first_ts else None,
        "last_timestamp_utc": last_ts.isoformat().replace("+00:00", "Z") if last_ts else None,
        "duration_seconds": duration,
        "cwd": cwd,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "turn_count": turns,
        "tool_call_count": tool_calls,
        "reasoning_item_count": reasoning_items,
        "prompt_ids": ",".join(sorted(prompt_ids)),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": max(0, input_tokens - cached),
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
        "cached_input_ratio": (cached / input_tokens) if input_tokens else None,
        "quota_used_percent_first": first_quota,
        "quota_used_percent_last": last_quota,
        "quota_delta_points": (last_quota - first_quota) if first_quota is not None and last_quota is not None else None,
        "quota_resets_at": reset_at,
    }


def write_outputs(rows: list[dict[str, Any]], root: Path) -> None:
    index = root / "index"
    index.mkdir(parents=True, exist_ok=True)
    db = index / "task_costs.sqlite"
    columns = list(rows[0]) if rows else list(analyze.__annotations__)
    schema = """
    CREATE TABLE IF NOT EXISTS session_costs (
      session_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
      first_timestamp_utc TEXT, last_timestamp_utc TEXT, duration_seconds REAL,
      cwd TEXT, model TEXT, reasoning_effort TEXT, turn_count INTEGER,
      tool_call_count INTEGER, reasoning_item_count INTEGER, prompt_ids TEXT,
      input_tokens INTEGER, cached_input_tokens INTEGER, uncached_input_tokens INTEGER,
      output_tokens INTEGER, reasoning_output_tokens INTEGER, total_tokens INTEGER,
      cached_input_ratio REAL, quota_used_percent_first REAL, quota_used_percent_last REAL,
      quota_delta_points REAL, quota_resets_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_costs_time ON session_costs(first_timestamp_utc);
    CREATE INDEX IF NOT EXISTS idx_costs_model ON session_costs(model, reasoning_effort);
    CREATE VIEW IF NOT EXISTS expensive_sessions AS
      SELECT * FROM session_costs ORDER BY total_tokens DESC;
    CREATE VIEW IF NOT EXISTS model_reasoning_summary AS
      SELECT model, reasoning_effort, COUNT(*) sessions,
             ROUND(AVG(total_tokens)) avg_total_tokens,
             ROUND(AVG(tool_call_count),1) avg_tool_calls,
             ROUND(AVG(duration_seconds),1) avg_duration_seconds,
             ROUND(AVG(quota_delta_points),3) avg_quota_delta_points
      FROM session_costs GROUP BY model, reasoning_effort;
    """
    with sqlite3.connect(db) as con:
        con.executescript(schema)
        keys = ["session_id","source_path","first_timestamp_utc","last_timestamp_utc","duration_seconds","cwd","model","reasoning_effort","turn_count","tool_call_count","reasoning_item_count","prompt_ids","input_tokens","cached_input_tokens","uncached_input_tokens","output_tokens","reasoning_output_tokens","total_tokens","cached_input_ratio","quota_used_percent_first","quota_used_percent_last","quota_delta_points","quota_resets_at"]
        sql = f"INSERT OR REPLACE INTO session_costs ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})"
        con.executemany(sql, [[row.get(k) for k in keys] for row in rows])
        con.commit()
    csv_path = index / "task_costs.csv"
    keys = list(rows[0].keys()) if rows else []
    if keys:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader(); writer.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser(description="Derive per-session Codex token/quota cost metrics from native rollouts.")
    p.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    p.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    args = p.parse_args()
    rows = [analyze(path) for path in sorted(args.source_root.rglob("*.jsonl"))]
    write_outputs(rows, args.archive_root)
    print(f"session_costs={len(rows)} db={args.archive_root / 'index/task_costs.sqlite'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
