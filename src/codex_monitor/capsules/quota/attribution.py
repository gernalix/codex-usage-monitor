"""Passive, approximate attribution of quota changes to native Codex sessions.

Never invokes a model. Session token counters are NOT provider billing units.
Only IDs, project basenames and explicit automation IDs are retained.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_SESSIONS = Path.home() / ".codex/sessions"
AUTOMATION_ID = re.compile(r"<automation_id>([A-Za-z0-9_.:-]{1,80})</automation_id>")
COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _extract_session(path: Path, start: float, end: float) -> dict[str, Any] | None:
    before: dict[str, int] | None = None
    after: dict[str, int] | None = None
    first_time: float | None = None
    session_id = path.stem[-36:]
    project = "sconosciuto"
    source = "non specificata"
    automation_ids: set[str] = set()
    user_turns = 0
    updates = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                kind = event.get("type")
                event_time = _timestamp(event.get("timestamp"))
                if kind == "session_meta":
                    session_id = str(payload.get("id") or session_id)
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        project = Path(cwd).name[:80]
                    source = str(payload.get("originator") or payload.get("source") or source)[:40]
                    if event_time is not None:
                        first_time = event_time
                elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                    if event_time is not None and start < event_time <= end:
                        user_turns += 1
                        for part in payload.get("content", []):
                            if not isinstance(part, dict):
                                continue
                            body = part.get("text")
                            if isinstance(body, str):
                                automation_ids.update(AUTOMATION_ID.findall(body))
                elif kind == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    totals = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(totals, dict) or event_time is None:
                        continue
                    counters = {key: int(totals.get(key, 0) or 0) for key in COUNTERS}
                    if event_time <= start:
                        before = counters
                    elif event_time <= end:
                        after = counters
                        updates += 1
    except (OSError, UnicodeError, ValueError) as exc:
        logging.warning("Skipping unreadable Codex usage rollout: %s", type(exc).__name__)
        return None

    if after is None:
        return None
    if before is None:
        # A fresh session starts at zero; an older session without a baseline is unknown.
        if first_time is None or first_time <= start:
            return None
        before = {key: 0 for key in COUNTERS}
    delta = {key: after[key] - before[key] for key in COUNTERS}
    if any(number < 0 for number in delta.values()) or delta["total_tokens"] <= 0:
        return None
    return {
        "session_id": session_id,
        "project": project,
        "source": source,
        "activation": "automatic" if automation_ids else ("unmarked_user_turn" if user_turns else "continuation"),
        "automation_ids": sorted(automation_ids),
        "token_events": updates,
        "input_tokens": delta["input_tokens"],
        "cached_input_tokens": delta["cached_input_tokens"],
        "uncached_input_tokens": max(0, delta["input_tokens"] - delta["cached_input_tokens"]),
        "output_tokens": delta["output_tokens"],
        "total_tokens": delta["total_tokens"],
        "thread_url": f"codex://threads/{session_id}",
    }


def inspect_rollouts(start_utc: str, end_utc: str, sessions_root: Path = DEFAULT_SESSIONS) -> dict[str, Any]:
    start, end = _timestamp(start_utc), _timestamp(end_utc)
    if start is None or end is None or end <= start:
        raise ValueError("Invalid attribution interval")
    if not sessions_root.is_dir():
        return {"status": "sessions_unavailable", "scanned": 0, "sessions": []}
    sessions: list[dict[str, Any]] = []
    scanned = 0
    for path in sessions_root.rglob("rollout-*.jsonl"):
        try:
            # Unmodified rollouts cannot contain token events from this interval.
            if path.stat().st_mtime < start - 120:
                continue
        except OSError:
            continue
        scanned += 1
        session = _extract_session(path, start, end)
        if session:
            sessions.append(session)
    sessions.sort(key=lambda item: item["total_tokens"], reverse=True)
    return {
        "status": "observed" if sessions else "no_local_token_events",
        "scanned": scanned,
        "sessions": sessions,
        "observed_total_tokens": sum(row["total_tokens"] for row in sessions),
        "observed_cached_input_tokens": sum(row["cached_input_tokens"] for row in sessions),
        "observed_uncached_input_tokens": sum(row["uncached_input_tokens"] for row in sessions),
    }


def build_attribution(con: sqlite3.Connection, snapshot_id: int, sessions_root: Path = DEFAULT_SESSIONS) -> dict[str, Any] | None:
    current = con.execute("SELECT * FROM quota_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if not current or current["acquisition_status"] != "ok":
        return None
    previous = con.execute(
        """SELECT * FROM quota_snapshots
           WHERE acquisition_status='ok' AND snapshot_id < ?
           ORDER BY acquired_at_utc DESC, snapshot_id DESC LIMIT 1""",
        (snapshot_id,),
    ).fetchone()
    if not previous:
        return None
    old_used, new_used = previous["weekly_used_percent"], current["weekly_used_percent"]
    if old_used is None or new_used is None or new_used <= old_used:
        return None
    if previous["weekly_reset_at_utc"] != current["weekly_reset_at_utc"]:
        return None
    start, end = previous["acquired_at_utc"], current["acquired_at_utc"]
    elapsed = (_timestamp(end) or 0) - (_timestamp(start) or 0)
    if not 0 < elapsed <= 48 * 3600:
        return None
    observed = inspect_rollouts(start, end, sessions_root)
    return {
        "schema": "codex.quota-attribution.v1",
        "snapshot_id": snapshot_id,
        "previous_snapshot_id": previous["snapshot_id"],
        "from_utc": start,
        "to_utc": end,
        "weekly_used_delta_pp": round(new_used - old_used, 4),
        "limitations": "Session token deltas are observational, not an exact allocation of provider quota; external/cloud sessions and billing delays may be missing.",
        **observed,
    }


def store_attribution(con: sqlite3.Connection, report: dict[str, Any]) -> None:
    con.execute(
        """INSERT INTO quota_attributions
           (snapshot_id,previous_snapshot_id,diagnostic_json,created_at_utc)
           VALUES (?,?,?,?)
           ON CONFLICT(snapshot_id) DO UPDATE SET diagnostic_json=excluded.diagnostic_json,
             previous_snapshot_id=excluded.previous_snapshot_id,created_at_utc=excluded.created_at_utc""",
        (
            report["snapshot_id"], report["previous_snapshot_id"],
            json.dumps(report, sort_keys=True, ensure_ascii=False),
            dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        ),
    )
    con.commit()


def attribution_lines(con: sqlite3.Connection, snapshot_id: int) -> list[str]:
    row = con.execute("SELECT diagnostic_json FROM quota_attributions WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if not row:
        return []
    report = json.loads(row["diagnostic_json"])
    if not report["sessions"]:
        return ["Attribution: nessun consumo locale individuato (altre sessioni o ritardo di contabilizzazione possibili)."]
    result = ["Chat attive nell'intervallo (token osservati, NON quota esatta):"]
    for session in report["sessions"][:3]:
        label = session["project"]
        if session["automation_ids"]:
            label += " [auto: " + ", ".join(session["automation_ids"][:2]) + "]"
        result.append(f"- {label}: {session['total_tokens']:,} token, {session['thread_url']}")
    return result
