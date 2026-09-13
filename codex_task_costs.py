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
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
SESSION_KEYS = [
    "session_id", "source_path", "first_timestamp_utc", "last_timestamp_utc", "duration_seconds",
    "cwd", "model", "reasoning_effort", "turn_count", "tool_call_count", "reasoning_item_count",
    "prompt_ids", "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "cached_input_ratio", "quota_used_percent_first",
    "quota_used_percent_last", "quota_delta_points", "quota_resets_at",
]
PROMPT_KEYS = [
    "session_id", "source_path", "prompt_seq", "prompt_id", "first_timestamp_utc", "last_timestamp_utc",
    "duration_seconds", "cwd", "model", "reasoning_effort", "tool_call_count", "reasoning_item_count",
    "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_output_tokens",
    "total_tokens", "cached_input_ratio", "quota_used_percent_first", "quota_used_percent_last",
    "quota_delta_points", "quota_resets_at",
]


def parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(ts: dt.datetime | None) -> str | None:
    return ts.isoformat().replace("+00:00", "Z") if ts else None


def intv(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_snapshot(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {key: intv(source.get(key)) for key in USAGE_FIELDS}


def usage_delta(end: dict[str, int], start: dict[str, int]) -> dict[str, int]:
    return {key: max(0, intv(end.get(key)) - intv(start.get(key))) for key in USAGE_FIELDS}


def prompt_id_from_user_message(top: Any, ptype: Any, payload: dict[str, Any]) -> str | None:
    if top != "event_msg" or ptype != "user_message":
        return None
    message = payload.get("message")
    if not isinstance(message, str):
        return None
    match = PROMPT_RE.search(message)
    return match.group(1) if match else None


def primary_quota(payload: dict[str, Any]) -> tuple[float | None, int | None]:
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None, None
    primary = limits.get("primary")
    if not isinstance(primary, dict) or primary.get("used_percent") is None:
        return None, None
    try:
        used = float(primary["used_percent"])
    except (TypeError, ValueError):
        return None, None
    reset = primary.get("resets_at")
    try:
        reset_at = int(reset) if reset is not None else None
    except (TypeError, ValueError):
        reset_at = None
    return used, reset_at


def finalize_prompt(active: dict[str, Any], session_id: str, source_path: str) -> dict[str, Any]:
    start_usage = active["start_usage"]
    end_usage = active["last_usage"]
    delta = usage_delta(end_usage, start_usage)
    input_tokens = delta["input_tokens"]
    cached = delta["cached_input_tokens"]
    first_quota = active.get("first_quota")
    last_quota = active.get("last_quota")
    start_ts = active.get("start_ts")
    end_ts = active.get("last_ts") or start_ts
    return {
        "session_id": session_id,
        "source_path": source_path,
        "prompt_seq": active["prompt_seq"],
        "prompt_id": active["prompt_id"],
        "first_timestamp_utc": iso(start_ts),
        "last_timestamp_utc": iso(end_ts),
        "duration_seconds": (end_ts - start_ts).total_seconds() if start_ts and end_ts else None,
        "cwd": active.get("cwd"),
        "model": active.get("model"),
        "reasoning_effort": active.get("reasoning_effort"),
        "tool_call_count": active["tool_calls"],
        "reasoning_item_count": active["reasoning_items"],
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": max(0, input_tokens - cached),
        "output_tokens": delta["output_tokens"],
        "reasoning_output_tokens": delta["reasoning_output_tokens"],
        "total_tokens": delta["total_tokens"],
        "cached_input_ratio": (cached / input_tokens) if input_tokens else None,
        "quota_used_percent_first": first_quota,
        "quota_used_percent_last": last_quota,
        "quota_delta_points": (last_quota - first_quota) if first_quota is not None and last_quota is not None else None,
        "quota_resets_at": active.get("quota_resets_at"),
    }


def analyze_with_prompts(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    session_id = path.stem
    first_ts = last_ts = None
    model = reasoning_effort = cwd = None
    prompt_ids: list[str] = []
    prompt_id_seen: set[str] = set()
    tool_calls = reasoning_items = turns = 0
    latest_usage = usage_snapshot({})
    first_quota = last_quota = None
    reset_at = None
    active: dict[str, Any] | None = None
    prompt_rows: list[dict[str, Any]] = []
    prompt_seq = 0

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
                if active is not None:
                    active["model"] = model or active.get("model")
                    active["reasoning_effort"] = reasoning_effort or active.get("reasoning_effort")
                    active["cwd"] = cwd or active.get("cwd")

            prompt_id = prompt_id_from_user_message(top, ptype, payload)
            if prompt_id is not None:
                if active is not None:
                    prompt_rows.append(finalize_prompt(active, session_id, str(path)))
                prompt_seq += 1
                if prompt_id not in prompt_id_seen:
                    prompt_ids.append(prompt_id)
                    prompt_id_seen.add(prompt_id)
                active = {
                    "prompt_seq": prompt_seq,
                    "prompt_id": prompt_id,
                    "start_ts": ts,
                    "last_ts": ts,
                    "cwd": cwd,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "tool_calls": 0,
                    "reasoning_items": 0,
                    "start_usage": dict(latest_usage),
                    "last_usage": dict(latest_usage),
                    "first_quota": last_quota,
                    "last_quota": last_quota,
                    "quota_resets_at": reset_at,
                }
                continue

            if active is not None and ts:
                active["last_ts"] = ts

            if ptype == "function_call":
                tool_calls += 1
                if active is not None:
                    active["tool_calls"] += 1
            if ptype == "reasoning":
                reasoning_items += 1
                if active is not None:
                    active["reasoning_items"] += 1

            if top == "event_msg" and ptype == "token_count":
                info = payload.get("info")
                if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                    latest_usage = usage_snapshot(info["total_token_usage"])
                    if active is not None:
                        active["last_usage"] = dict(latest_usage)
                q, q_reset = primary_quota(payload)
                if q is not None:
                    if first_quota is None:
                        first_quota = q
                    last_quota = q
                    reset_at = q_reset or reset_at
                    if active is not None:
                        if active.get("first_quota") is None:
                            active["first_quota"] = q
                        active["last_quota"] = q
                        active["quota_resets_at"] = q_reset or active.get("quota_resets_at")

    if active is not None:
        prompt_rows.append(finalize_prompt(active, session_id, str(path)))

    duration = (last_ts - first_ts).total_seconds() if first_ts and last_ts else None
    input_tokens = latest_usage["input_tokens"]
    cached = latest_usage["cached_input_tokens"]
    session_row = {
        "session_id": session_id,
        "source_path": str(path),
        "first_timestamp_utc": iso(first_ts),
        "last_timestamp_utc": iso(last_ts),
        "duration_seconds": duration,
        "cwd": cwd,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "turn_count": turns,
        "tool_call_count": tool_calls,
        "reasoning_item_count": reasoning_items,
        "prompt_ids": ",".join(prompt_ids),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": max(0, input_tokens - cached),
        "output_tokens": latest_usage["output_tokens"],
        "reasoning_output_tokens": latest_usage["reasoning_output_tokens"],
        "total_tokens": latest_usage["total_tokens"],
        "cached_input_ratio": (cached / input_tokens) if input_tokens else None,
        "quota_used_percent_first": first_quota,
        "quota_used_percent_last": last_quota,
        "quota_delta_points": (last_quota - first_quota) if first_quota is not None and last_quota is not None else None,
        "quota_resets_at": reset_at,
    }
    return session_row, prompt_rows


def analyze(path: Path) -> dict[str, Any]:
    return analyze_with_prompts(path)[0]


def write_csv(path: Path, rows: list[dict[str, Any]], keys: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows([{key: row.get(key) for key in keys} for row in rows])


def write_outputs(rows: list[dict[str, Any]], prompt_rows: list[dict[str, Any]], root: Path) -> None:
    index = root / "index"
    index.mkdir(parents=True, exist_ok=True)
    db = index / "task_costs.sqlite"
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
    CREATE TABLE IF NOT EXISTS prompt_costs (
      session_id TEXT NOT NULL, source_path TEXT NOT NULL, prompt_seq INTEGER NOT NULL, prompt_id TEXT NOT NULL,
      first_timestamp_utc TEXT, last_timestamp_utc TEXT, duration_seconds REAL,
      cwd TEXT, model TEXT, reasoning_effort TEXT, tool_call_count INTEGER, reasoning_item_count INTEGER,
      input_tokens INTEGER, cached_input_tokens INTEGER, uncached_input_tokens INTEGER,
      output_tokens INTEGER, reasoning_output_tokens INTEGER, total_tokens INTEGER, cached_input_ratio REAL,
      quota_used_percent_first REAL, quota_used_percent_last REAL, quota_delta_points REAL, quota_resets_at INTEGER,
      PRIMARY KEY(source_path, prompt_seq)
    );
    CREATE INDEX IF NOT EXISTS idx_prompt_costs_prompt ON prompt_costs(prompt_id, first_timestamp_utc);
    CREATE INDEX IF NOT EXISTS idx_prompt_costs_model ON prompt_costs(model, reasoning_effort);
    CREATE VIEW IF NOT EXISTS expensive_sessions AS SELECT * FROM session_costs ORDER BY total_tokens DESC;
    CREATE VIEW IF NOT EXISTS expensive_prompts AS SELECT * FROM prompt_costs ORDER BY total_tokens DESC;
    CREATE VIEW IF NOT EXISTS model_reasoning_summary AS
      SELECT model, reasoning_effort, COUNT(*) sessions,
             ROUND(AVG(total_tokens)) avg_total_tokens,
             ROUND(AVG(tool_call_count),1) avg_tool_calls,
             ROUND(AVG(duration_seconds),1) avg_duration_seconds,
             ROUND(AVG(quota_delta_points),3) avg_quota_delta_points
      FROM session_costs GROUP BY model, reasoning_effort;
    CREATE VIEW IF NOT EXISTS prompt_model_reasoning_summary AS
      SELECT model, reasoning_effort, COUNT(*) prompts,
             ROUND(AVG(total_tokens)) avg_total_tokens,
             ROUND(AVG(tool_call_count),1) avg_tool_calls,
             ROUND(AVG(duration_seconds),1) avg_duration_seconds,
             ROUND(AVG(quota_delta_points),3) avg_quota_delta_points
      FROM prompt_costs GROUP BY model, reasoning_effort;
    """
    with sqlite3.connect(db) as con:
        con.executescript(schema)
        con.execute("DELETE FROM prompt_costs")
        session_sql = f"INSERT OR REPLACE INTO session_costs ({','.join(SESSION_KEYS)}) VALUES ({','.join('?' for _ in SESSION_KEYS)})"
        prompt_sql = f"INSERT OR REPLACE INTO prompt_costs ({','.join(PROMPT_KEYS)}) VALUES ({','.join('?' for _ in PROMPT_KEYS)})"
        con.executemany(session_sql, [[row.get(key) for key in SESSION_KEYS] for row in rows])
        con.executemany(prompt_sql, [[row.get(key) for key in PROMPT_KEYS] for row in prompt_rows])
        con.commit()
    write_csv(index / "task_costs.csv", rows, SESSION_KEYS)
    write_csv(index / "prompt_costs.csv", prompt_rows, PROMPT_KEYS)


def main() -> int:
    p = argparse.ArgumentParser(description="Derive per-session and per-PROMPT_ID Codex token/quota cost metrics from native rollouts.")
    p.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    p.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    p.add_argument("--prompt-id", help="print exact per-prompt rows for this PROMPT_ID after rebuilding metrics")
    p.add_argument("--json", action="store_true", help="emit matching --prompt-id rows as JSON")
    args = p.parse_args()

    rows: list[dict[str, Any]] = []
    prompt_rows: list[dict[str, Any]] = []
    for path in sorted(args.source_root.rglob("*.jsonl")):
        session_row, prompts = analyze_with_prompts(path)
        rows.append(session_row)
        prompt_rows.extend(prompts)
    write_outputs(rows, prompt_rows, args.archive_root)

    if args.prompt_id:
        matches = [row for row in prompt_rows if row["prompt_id"] == args.prompt_id]
        if args.json:
            print(json.dumps(matches, ensure_ascii=False, sort_keys=True))
        else:
            for row in matches:
                print(
                    f"PROMPT_ID={row['prompt_id']} total={row['total_tokens']} input={row['input_tokens']} "
                    f"cached={row['cached_input_tokens']} uncached={row['uncached_input_tokens']} "
                    f"output={row['output_tokens']} reasoning={row['reasoning_output_tokens']} "
                    f"tools={row['tool_call_count']} duration={row['duration_seconds']}s"
                )
        return 0 if matches else 1

    print(
        f"session_costs={len(rows)} prompt_costs={len(prompt_rows)} "
        f"db={args.archive_root / 'index/task_costs.sqlite'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
