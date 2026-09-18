#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

from codex_monitor.capsules.session_archive import api as archive
from codex_monitor.capsules.quota import api as quota


VERSION = "2026.09.05"
DEFAULT_SOURCE_ROOT = Path.home() / ".codex/sessions"
DEFAULT_STATE_DIR = Path.home() / ".local/state/codex-usage-publisher"
DEFAULT_DATA_REPO = Path.home() / "projects/codex-usage"
DEFAULT_DATA_REMOTE = "https://github.com/gernalix/codex-usage"
DEFAULT_QUOTA_DB = Path.home() / ".local/share/codex-usage-monitor/codex_usage_monitor.db"
SOURCE_SNAPSHOT_SCHEMA = 1


class PublisherError(RuntimeError):
    pass


class ExclusiveLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "ExclusiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.handle.write(f"{os.getpid()}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def utc_stamp(value: dt.datetime | None = None) -> str:
    value = value or dt.datetime.now(dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = archive.parse_ts(value)
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run(cmd: list[str], cwd: Path | None = None, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)


def git_ok(cmd: list[str], cwd: Path, *, timeout: int = 120) -> None:
    result = run(cmd, cwd, timeout=timeout)
    if result.returncode != 0:
        raise PublisherError(f"{' '.join(cmd)} failed: {archive.redact_text(result.stderr or result.stdout)}")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write(path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))


def connect_state(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "publisher.sqlite"
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_chats (
            chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            first_seen_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cycles (
            cycle_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            prompt_id TEXT,
            turn_id TEXT,
            final_event_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            completed_at_utc TEXT,
            published_commit TEXT,
            telegram_sent INTEGER NOT NULL DEFAULT 0,
            updated_at_utc TEXT NOT NULL
        );
        """
    )
    con.commit()
    return con


def _source_snapshot(paths: list[Path]) -> list[dict[str, int | str]] | None:
    rows: list[dict[str, int | str]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            return None
        rows.append(
            {
                "path": str(path),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "ctime_ns": int(stat.st_ctime_ns),
            }
        )
    return rows


def _source_snapshot_path(state_dir: Path) -> Path:
    return state_dir / "source-snapshot.json"


def _source_snapshot_matches(
    state_dir: Path,
    generation: str,
    rows: list[dict[str, int | str]],
) -> bool:
    path = _source_snapshot_path(state_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("schema") == SOURCE_SNAPSHOT_SCHEMA
        and payload.get("generation") == generation
        and payload.get("files") == rows
    )


def _write_source_snapshot(
    state_dir: Path,
    generation: str,
    rows: list[dict[str, int | str]],
) -> None:
    atomic_write(
        _source_snapshot_path(state_dir),
        json.dumps(
            {
                "schema": SOURCE_SNAPSHOT_SCHEMA,
                "generation": generation,
                "files": rows,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )


def _publisher_has_pending_state(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM cycles WHERE published_commit IS NULL OR telegram_sent=0 LIMIT 1"
    ).fetchone()
    return row is not None


def source_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_output_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return archive.extract_text(payload)


def token_usage(payload: dict[str, Any]) -> dict[str, int | None]:
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
    total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
    src = usage or total
    keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    result: dict[str, int | None] = {}
    for key in keys:
        try:
            result[key] = int(src[key]) if src.get(key) is not None else None
        except (TypeError, ValueError):
            result[key] = None
    if result.get("input_tokens") is not None and result.get("cached_input_tokens") is not None:
        result["uncached_input_tokens"] = max(0, int(result["input_tokens"] or 0) - int(result["cached_input_tokens"] or 0))
    else:
        result["uncached_input_tokens"] = None
    return result


def quota_from_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    primary = limits.get("primary") if isinstance(limits.get("primary"), dict) else {}
    used = quota.parse_float(primary.get("used_percent"))
    remaining = quota.parse_float(primary.get("remaining_percent"))
    if remaining is None and used is not None:
        remaining = quota.clamp_percent(100.0 - used)
    return {
        "weekly_used_percent": quota.clamp_percent(used) if used is not None else None,
        "weekly_remaining_percent": quota.clamp_percent(remaining) if remaining is not None else None,
        "weekly_reset_at_utc": quota.epoch_to_utc_iso(primary.get("resets_at")),
    }


def prompt_id_from_text(text: str) -> str | None:
    return archive.prompt_id_from_text(text)


def json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def explicit_paths_from_payload(payload: dict[str, Any]) -> list[str]:
    args = json_obj(payload.get("arguments")) or json_obj(payload.get("input"))
    paths: list[str] = []
    for key in ("workdir", "cwd", "path", "file"):
        value = args.get(key) or payload.get(key)
        if isinstance(value, str) and value.startswith("/"):
            paths.append(value)
    target = args.get("target")
    if isinstance(target, dict):
        value = target.get("path")
        if isinstance(value, str) and value.startswith("/"):
            paths.append(value)
    return paths


def apply_patch_paths_from_payload(payload: dict[str, Any], cwd: str | None) -> list[str]:
    raw = payload.get("arguments")
    if not isinstance(raw, str):
        raw = payload.get("input")
    if not isinstance(raw, str):
        return explicit_paths_from_payload(payload)

    args = json_obj(raw)
    patch = args.get("patch") if isinstance(args.get("patch"), str) else raw
    base = Path(cwd).expanduser() if cwd else None
    paths: list[str] = []
    for match in re.finditer(r"(?m)^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", patch):
        value = match.group(1).strip()
        path = Path(value).expanduser()
        if not path.is_absolute():
            if base is None:
                continue
            path = base / path
        paths.append(str(path))
    return paths


def status_from_final(text: str) -> str:
    first = next((line.strip().upper() for line in text.splitlines() if line.strip()), "")
    return first if first in {"PASS", "FAIL", "PARTIAL", "BLOCKED_REPO_PUBLIC"} else "UNKNOWN"


def get_chat_id(con: sqlite3.Connection, session_id: str) -> int:
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT chat_id FROM session_chats WHERE session_id=?", (session_id,)).fetchone()
        if row:
            con.commit()
            return int(row["chat_id"])
        cur = con.execute(
            "INSERT INTO session_chats (session_id,first_seen_utc) VALUES (?,?)",
            (session_id, utc_stamp()),
        )
        con.commit()
        return int(cur.lastrowid)
    except Exception:
        con.rollback()
        raise


def parse_session(path: Path, con: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    session_id = archive.first_uuid_from_path(path) or path.stem
    chat_id: int | None = None
    current: dict[str, Any] | None = None
    cycles: list[dict[str, Any]] = []
    chat_events: list[dict[str, Any]] = []
    final_by_turn: dict[str, str] = {}
    session_cwd: str | None = None
    raw_lines = path.read_text(encoding="utf-8", errors="replace")
    sha = digest_text(raw_lines)

    for line_no, line in enumerate(raw_lines.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        top = str(obj.get("type") or "")
        ptype = str(payload.get("type") or "")
        ts = parse_ts(obj.get("timestamp"))
        if top == "session_meta":
            session_id = str(payload.get("session_id") or payload.get("id") or session_id)
            if isinstance(payload.get("cwd"), str):
                session_cwd = payload["cwd"]
            chat_id = get_chat_id(con, session_id)
        if chat_id is None:
            chat_id = get_chat_id(con, session_id)
        if top == "turn_context":
            collab = payload.get("collaboration_mode") if isinstance(payload.get("collaboration_mode"), dict) else {}
            settings = collab.get("settings") if isinstance(collab.get("settings"), dict) else {}
            current = {
                "turn_id": str(payload.get("turn_id") or f"line-{line_no}"),
                "root_turn_id": payload.get("root_turn_id"),
                "started_at_utc": utc_stamp(ts) if ts else None,
                "model": payload.get("model") or settings.get("model"),
                "reasoning_effort": settings.get("reasoning_effort") or payload.get("effort"),
                "cwd": payload.get("cwd") or session_cwd,
                "repo_paths": [],
                "repo_write_paths": [],
                "prompt_text": "",
                "prompt_id": None,
                "events": [],
                "tool_calls": 0,
                "tool_calls_by_type": {},
                "turns": 1,
                "token": {},
                "quota_first": None,
                "quota_last": None,
            }
        event = {
            "timestamp_utc": utc_stamp(ts) if ts else None,
            "top_type": top,
            "subtype": ptype or None,
            "role": payload.get("role") if isinstance(payload.get("role"), str) else None,
            "tool_name": payload.get("name") if ptype in {"function_call", "custom_tool_call"} else None,
            "content_text": archive.redact_text(extract_output_text(payload)),
        }
        chat_events.append(event)
        if current is not None:
            current["events"].append(event)
            if ptype in {"function_call", "custom_tool_call"}:
                current["tool_calls"] += 1
                name = str(payload.get("name") or ptype)
                current["tool_calls_by_type"][name] = current["tool_calls_by_type"].get(name, 0) + 1
                payload_paths = (
                    apply_patch_paths_from_payload(payload, current.get("cwd"))
                    if name == "apply_patch"
                    else explicit_paths_from_payload(payload)
                )
                current["repo_paths"].extend(payload_paths)
                if name == "apply_patch":
                    current["repo_write_paths"].extend(payload_paths)
            if ptype == "message" and payload.get("role") == "user" and not current["prompt_text"]:
                current["prompt_text"] = archive.redact_text(extract_output_text(payload))
                current["prompt_id"] = prompt_id_from_text(current["prompt_text"])
            if top == "event_msg" and ptype == "token_count":
                current["token"] = token_usage(payload)
                q = quota_from_event(payload)
                if q:
                    if current["quota_first"] is None:
                        current["quota_first"] = q
                    current["quota_last"] = q
            if ptype == "message" and payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                turn_id = ((payload.get("internal_chat_message_metadata_passthrough") or {}).get("turn_id"))
                if isinstance(turn_id, str):
                    final_by_turn[turn_id] = archive.redact_text(extract_output_text(payload))
        if top == "event_msg" and ptype == "task_complete" and current is not None:
            turn_id = str(payload.get("turn_id") or current["turn_id"])
            final = archive.redact_text(str(payload.get("last_agent_message") or final_by_turn.get(turn_id) or ""))
            completed = parse_ts(payload.get("completed_at")) or ts
            started = parse_ts(payload.get("started_at"))
            if started:
                current["started_at_utc"] = utc_stamp(started)
            completed_text = utc_stamp(completed) if completed else utc_stamp(ts) if ts else None
            cycle_key = digest_text(f"{session_id}:{turn_id}:{line_no}")[:24]
            q_first = current.get("quota_first") or {}
            q_last = current.get("quota_last") or {}
            metrics = {
                "schema": "codex-usage.prompt-metrics.v1",
                "cycle_key": cycle_key,
                "prompt_id": current.get("prompt_id"),
                "chat_id": chat_id,
                "native_session_id": session_id,
                "turn_id": turn_id,
                "source_path": str(path),
                "timestamp_start_utc": current.get("started_at_utc"),
                "timestamp_end_utc": completed_text,
                "duration_seconds": (int(payload.get("duration_ms")) / 1000.0) if payload.get("duration_ms") is not None else None,
                "model": current.get("model"),
                "reasoning_effort": current.get("reasoning_effort"),
                "turn_count": current.get("turns"),
                "tool_call_count": current.get("tool_calls"),
                "tool_calls_by_type": current.get("tool_calls_by_type"),
                **(current.get("token") or {}),
                "cache_ratio": (
                    (current["token"].get("cached_input_tokens") or 0) / current["token"]["input_tokens"]
                    if current.get("token") and current["token"].get("input_tokens")
                    else None
                ),
                "quota_weekly_before": q_first.get("weekly_remaining_percent"),
                "quota_weekly_after": q_last.get("weekly_remaining_percent"),
                "quota_delta_observed": (
                    q_last.get("weekly_remaining_percent") - q_first.get("weekly_remaining_percent")
                    if q_last.get("weekly_remaining_percent") is not None and q_first.get("weekly_remaining_percent") is not None
                    else None
                ),
                "quota_reset_at_utc": q_last.get("weekly_reset_at_utc"),
                "prompt_text_redacted": current.get("prompt_text") or None,
                "final_response_redacted": final,
                "status": status_from_final(final),
                "repo_project": current.get("cwd"),
                "repo_paths": current.get("repo_paths") or [],
                "repo_write_paths": current.get("repo_write_paths") or [],
            }
            cycles.append({"metrics": metrics, "events": current["events"], "final_event_id": f"{path}:{line_no}", "source_sha256": sha})
            current = None
    return cycles, chat_events


def ensure_repo(repo: Path, remote: str) -> None:
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        git_ok(["git", "clone", remote, str(repo)], repo.parent, timeout=240)
    if not (repo / ".git").exists():
        raise PublisherError(f"not a git repository: {repo}")
    remote_url = run(["git", "config", "--get", "remote.origin.url"], repo).stdout.strip()
    if remote_url != remote and remote_url.rstrip(".git") != remote.rstrip(".git"):
        raise PublisherError(f"unexpected codex-usage remote: {remote_url}")
    remote_main = run(["git", "ls-remote", "--heads", "origin", "main"], repo, timeout=60).stdout.strip()
    if remote_main:
        git_ok(["git", "fetch", "origin", "main"], repo, timeout=180)
    local_main = run(["git", "rev-parse", "--verify", "main"], repo, timeout=30).returncode == 0
    if local_main or remote_main:
        git_ok(["git", "checkout", "main"], repo)
    else:
        git_ok(["git", "checkout", "-B", "main"], repo)
    if remote_main:
        git_ok(["git", "pull", "--ff-only", "origin", "main"], repo, timeout=180)


def assert_private_repo(remote: str) -> None:
    result = run(["gh", "repo", "view", remote, "--json", "visibility,isPrivate"], timeout=30)
    if result.returncode != 0:
        return
    data = json.loads(result.stdout)
    if data.get("visibility") == "PUBLIC" or data.get("isPrivate") is False:
        raise PublisherError("BLOCKED_REPO_PUBLIC")


def prompt_dir(metrics: dict[str, Any]) -> Path:
    prompt_id = metrics.get("prompt_id")
    if prompt_id:
        return Path("prompts") / str(prompt_id)
    return Path("prompts/unassigned") / str(metrics["cycle_key"])


def chat_metrics(chat_id: int, cycles: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [c["metrics"].get("prompt_id") for c in cycles if c["metrics"].get("prompt_id")]
    totals: dict[str, int] = {}
    for key in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens", "tool_call_count"):
        values = [c["metrics"].get(key) for c in cycles if c["metrics"].get(key) is not None]
        totals[key] = int(sum(values)) if values else 0
    return {
        "schema": "codex-usage.chat-metrics.v1",
        "chat_id": chat_id,
        "native_session_id": cycles[0]["metrics"]["native_session_id"],
        "prompt_ids": ids,
        "cycle_count": len(cycles),
        "first_timestamp_utc": min((c["metrics"].get("timestamp_start_utc") for c in cycles if c["metrics"].get("timestamp_start_utc")), default=None),
        "last_timestamp_utc": max((c["metrics"].get("timestamp_end_utc") for c in cycles if c["metrics"].get("timestamp_end_utc")), default=None),
        "totals": totals,
    }


def dedupe_cycle_keys(cycles: list[dict[str, Any]]) -> None:
    seen: dict[str, list[dict[str, Any]]] = {}
    for cycle in cycles:
        seen.setdefault(str(cycle["metrics"]["cycle_key"]), []).append(cycle)
    for key, items in seen.items():
        if len(items) < 2:
            continue
        for cycle in items:
            suffix = digest_text(str(cycle["metrics"].get("source_path") or cycle["final_event_id"]))[:8]
            cycle["metrics"]["cycle_key"] = f"{key}-{suffix}"


def quota_index(quota_db: Path) -> list[dict[str, Any]]:
    if not quota_db.exists() or not archive.sqlite_has_table(quota_db, "quota_snapshots"):
        return []
    uri = f"file:{quota_db}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as con:
        con.row_factory = sqlite3.Row
        return [archive.redact_obj(dict(row)) for row in con.execute("SELECT * FROM quota_overview ORDER BY acquired_at_utc DESC LIMIT 500")]


def export_repo(repo: Path, cycles: list[dict[str, Any]], chat_events_by_id: dict[int, list[dict[str, Any]]], quota_db: Path) -> None:
    repo.joinpath("metadata").mkdir(parents=True, exist_ok=True)
    write_json(
        repo / "metadata/schema.json",
        {
            "schema": "codex-usage.schema.v1",
            "generated_by": f"codex_usage_publisher {VERSION}",
            "final_response_signal": "event_msg payload.type=task_complete; assistant final text from payload.last_agent_message or response_item phase=final_answer",
            "redaction": "codex_session_archive.redact_text/redact_obj",
        },
    )
    prompt_rows = []
    for cycle in cycles:
        metrics = cycle["metrics"]
        rel = prompt_dir(metrics)
        write_json(repo / rel / "metrics.json", metrics)
        write_jsonl(repo / rel / "transcript.jsonl", cycle["events"])
        if metrics.get("prompt_id"):
            stale = repo / "prompts/unassigned" / str(metrics["cycle_key"])
            if stale.exists():
                shutil.rmtree(stale)
        prompt_rows.append(
            {
                "prompt_id": metrics.get("prompt_id"),
                "chat_id": metrics["chat_id"],
                "cycle_key": metrics["cycle_key"],
                "native_session_id": metrics["native_session_id"],
                "timestamp_end_utc": metrics.get("timestamp_end_utc"),
                "status": metrics.get("status"),
                "path": rel.as_posix(),
            }
        )
    by_chat: dict[int, list[dict[str, Any]]] = {}
    for cycle in cycles:
        by_chat.setdefault(int(cycle["metrics"]["chat_id"]), []).append(cycle)
    chat_rows = []
    for chat_id, chat_cycles in sorted(by_chat.items()):
        metrics = chat_metrics(chat_id, chat_cycles)
        write_json(repo / "chats" / str(chat_id) / "metrics.json", metrics)
        write_jsonl(repo / "chats" / str(chat_id) / "transcript.jsonl", chat_events_by_id.get(chat_id, []))
        chat_rows.append({"chat_id": chat_id, "native_session_id": metrics["native_session_id"], "cycle_count": metrics["cycle_count"], "prompt_ids": metrics["prompt_ids"], "path": f"chats/{chat_id}"})
    quota_rows = quota_index(quota_db)
    write_jsonl(repo / "index/prompts.jsonl", sorted(prompt_rows, key=lambda r: (r.get("timestamp_end_utc") or "", str(r.get("cycle_key")))))
    write_jsonl(repo / "index/chats.jsonl", chat_rows)
    write_jsonl(repo / "index/quota.jsonl", quota_rows)
    write_json(
        repo / "index/latest.json",
        {
            "schema": "codex-usage.latest.v1",
            "updated_at_utc": max((row.get("timestamp_end_utc") for row in prompt_rows if row.get("timestamp_end_utc")), default=None),
            "prompt_count": len(prompt_rows),
            "chat_count": len(chat_rows),
            "final_response_cycle_count": len(cycles),
            "latest_prompt": prompt_rows[-1] if prompt_rows else None,
            "latest_quota": quota_rows[0] if quota_rows else None,
        },
    )


def send_batch_telegram(cycles: list[dict[str, Any]], dry_run: bool) -> bool:
    missing_by_chat: dict[int, list[str]] = {}
    for cycle in cycles:
        if cycle["metrics"].get("prompt_id"):
            continue
        missing_by_chat.setdefault(int(cycle["metrics"]["chat_id"]), []).append(str(cycle["metrics"]["cycle_key"]))
    if not missing_by_chat:
        return True
    if len(missing_by_chat) == 1:
        chat_id, cycle_keys = next(iter(missing_by_chat.items()))
        first_key = cycle_keys[0]
        last_key = cycle_keys[-1]
        message = (
            f"Anomalia Codex usage: {len(cycle_keys)} cicli final-response senza PROMPT_ID nella chat {chat_id}.\n"
            f"Primo ciclo: {first_key}\nUltimo ciclo: {last_key}"
        )
    else:
        message = "; ".join(f"chat {chat}: {len(keys)} cicli senza PROMPT_ID" for chat, keys in sorted(missing_by_chat.items()))
    if dry_run:
        print(json.dumps({"telegram": "dry_run", "message": message}, sort_keys=True))
        return True
    quota.send_telegram(quota.build_config(None), "Codex usage", message)
    return True


def command_run(
    args: argparse.Namespace,
    *,
    assert_private_repo_fn=assert_private_repo,
    connect_state_fn=connect_state,
    parse_session_fn=parse_session,
    ensure_repo_fn=ensure_repo,
    export_repo_fn=export_repo,
    run_fn=run,
    send_batch_telegram_fn=send_batch_telegram,
    git_ok_fn=git_ok,
    utc_stamp_fn=utc_stamp,
    source_generation: str | None = None,
) -> int:
    assert_private_repo_fn(args.remote)
    state_dir = Path(args.state_dir).expanduser()
    source_root = Path(args.source_root).expanduser()
    repo = Path(args.data_repo).expanduser()
    quota_db = Path(args.quota_db).expanduser()
    with ExclusiveLock(state_dir / "publisher.lock"):
        with connect_state_fn(state_dir) as con:
            source_paths = sorted(source_root.glob("**/*.jsonl"))
            source_snapshot = _source_snapshot(source_paths)
            if (
                source_generation
                and source_snapshot is not None
                and not _publisher_has_pending_state(con)
                and _source_snapshot_matches(state_dir, source_generation, source_snapshot)
            ):
                counts = con.execute(
                    "SELECT COUNT(*) cycles, SUM(prompt_id IS NOT NULL) prompt_ids FROM cycles"
                ).fetchone()
                chats = con.execute("SELECT COUNT(*) chats FROM session_chats").fetchone()
                print(
                    json.dumps(
                        {
                            "status": "noop_unchanged_sources",
                            "chats": int(chats["chats"] or 0),
                            "cycles": int(counts["cycles"] or 0),
                            "prompt_ids": int(counts["prompt_ids"] or 0),
                        },
                        sort_keys=True,
                    )
                )
                return 0

            cycles: list[dict[str, Any]] = []
            chat_events: dict[int, list[dict[str, Any]]] = {}
            for path in source_paths:
                parsed, events = parse_session_fn(path, con)
                for cycle in parsed:
                    cycles.append(cycle)
                if parsed:
                    chat_id = int(parsed[0]["metrics"]["chat_id"])
                    chat_events[chat_id] = events
            dedupe_cycle_keys(cycles)
            pending_cycles = []
            for cycle in cycles:
                row = con.execute(
                    "SELECT source_sha256,published_commit,prompt_id FROM cycles WHERE cycle_key=?",
                    (cycle["metrics"]["cycle_key"],),
                ).fetchone()
                if (
                    row is None
                    or row["published_commit"] is None
                    or row["source_sha256"] != cycle["source_sha256"]
                    or row["prompt_id"] != cycle["metrics"].get("prompt_id")
                ):
                    pending_cycles.append(cycle)
            ensure_repo_fn(repo, args.remote)
            export_repo_fn(repo, cycles, chat_events, quota_db)
            status = run_fn(["git", "status", "--short"], repo).stdout
            if not status.strip():
                current_commit = run_fn(["git", "rev-parse", "HEAD"], repo).stdout.strip()
                retry_cycles = []
                for cycle in cycles:
                    row = con.execute(
                        "SELECT published_commit,telegram_sent FROM cycles WHERE cycle_key=?",
                        (cycle["metrics"]["cycle_key"],),
                    ).fetchone()
                    if row and row["published_commit"] and not row["telegram_sent"]:
                        retry_cycles.append(cycle)
                    elif row is None or row["published_commit"] is None:
                        m = cycle["metrics"]
                        con.execute(
                            """
                            INSERT INTO cycles (cycle_key,session_id,chat_id,prompt_id,turn_id,final_event_id,source_path,source_sha256,cycle_sha256,fingerprint_schema,completed_at_utc,published_commit,telegram_sent,updated_at_utc)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                            ON CONFLICT(cycle_key) DO UPDATE SET
                                prompt_id=excluded.prompt_id,
                                source_sha256=excluded.source_sha256,
                                cycle_sha256=excluded.cycle_sha256,
                                fingerprint_schema=excluded.fingerprint_schema,
                                published_commit=excluded.published_commit,
                                updated_at_utc=excluded.updated_at_utc
                            """,
                            (m["cycle_key"], m["native_session_id"], m["chat_id"], m.get("prompt_id"), m.get("turn_id"), cycle["final_event_id"], m["source_path"], cycle["source_sha256"], cycle.get("cycle_sha256", cycle["source_sha256"]), cycle.get("fingerprint_schema"), m.get("timestamp_end_utc"), current_commit, utc_stamp_fn()),
                        )
                        retry_cycles.append(cycle)
                if retry_cycles:
                    con.commit()
                if retry_cycles:
                    retry_keys = {c["metrics"]["cycle_key"] for c in retry_cycles}
                    send_batch_telegram_fn(retry_cycles, args.dry_run_telegram)
                    con.execute("UPDATE cycles SET telegram_sent=1 WHERE cycle_key IN (%s)" % ",".join("?" for _ in retry_keys), tuple(retry_keys))
                    con.commit()
                    if source_generation and source_snapshot is not None:
                        _write_source_snapshot(state_dir, source_generation, source_snapshot)
                    print(json.dumps({"status": "telegram_retried", "chats": len({c["metrics"]["chat_id"] for c in retry_cycles}), "cycles": len(retry_cycles)}, sort_keys=True))
                    return 0
                if source_generation and source_snapshot is not None:
                    _write_source_snapshot(state_dir, source_generation, source_snapshot)
                print(json.dumps({"status": "noop", "chats": len(chat_events), "cycles": len(cycles), "prompt_ids": len({c["metrics"].get("prompt_id") for c in cycles if c["metrics"].get("prompt_id")})}, sort_keys=True))
                return 0
            if args.no_push:
                print(json.dumps({"status": "dry_run", "changed": status.strip().splitlines()}, sort_keys=True))
                return 0
            git_ok_fn(["git", "add", "index", "prompts", "chats", "metadata"], repo)
            status = run_fn(["git", "status", "--short"], repo).stdout
            if not status.strip():
                print(json.dumps({"status": "noop"}, sort_keys=True))
                return 0
            changed_cycle_keys = {c["metrics"]["cycle_key"] for c in pending_cycles}
            if len(pending_cycles) == 1 and pending_cycles[0]["metrics"].get("prompt_id"):
                msg = f"usage: prompt {pending_cycles[0]['metrics']['prompt_id']} chat {pending_cycles[0]['metrics']['chat_id']}"
            else:
                msg = f"usage: publish {len(pending_cycles)} prompt cycles"
            git_ok_fn(["git", "commit", "-m", msg], repo)
            git_ok_fn(["git", "push", "origin", "main"], repo, timeout=240)
            commit = run_fn(["git", "rev-parse", "HEAD"], repo).stdout.strip()
            for cycle in pending_cycles:
                m = cycle["metrics"]
                con.execute(
                    """
                    INSERT INTO cycles (cycle_key,session_id,chat_id,prompt_id,turn_id,final_event_id,source_path,source_sha256,cycle_sha256,fingerprint_schema,completed_at_utc,published_commit,telegram_sent,updated_at_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                    ON CONFLICT(cycle_key) DO UPDATE SET
                        prompt_id=excluded.prompt_id,
                        source_sha256=excluded.source_sha256,
                        cycle_sha256=excluded.cycle_sha256,
                        fingerprint_schema=excluded.fingerprint_schema,
                        published_commit=excluded.published_commit,
                        updated_at_utc=excluded.updated_at_utc
                    """,
                    (m["cycle_key"], m["native_session_id"], m["chat_id"], m.get("prompt_id"), m.get("turn_id"), cycle["final_event_id"], m["source_path"], cycle["source_sha256"], cycle.get("cycle_sha256", cycle["source_sha256"]), cycle.get("fingerprint_schema"), m.get("timestamp_end_utc"), commit, utc_stamp_fn()),
                )
            con.commit()
            if pending_cycles:
                send_batch_telegram_fn(pending_cycles, args.dry_run_telegram)
                con.execute("UPDATE cycles SET telegram_sent=1 WHERE cycle_key IN (%s)" % ",".join("?" for _ in changed_cycle_keys), tuple(changed_cycle_keys))
                con.commit()
            if source_generation and source_snapshot is not None:
                _write_source_snapshot(state_dir, source_generation, source_snapshot)
            print(json.dumps({"status": "published", "commit": commit, "chats": len(chat_events), "cycles": len(cycles), "published_cycles": len(pending_cycles)}, sort_keys=True))
            return 0


def command_status(args: argparse.Namespace) -> int:
    with connect_state(Path(args.state_dir).expanduser()) as con:
        row = con.execute("SELECT COUNT(*) chats FROM session_chats").fetchone()
        cycles = con.execute("SELECT COUNT(*) cycles, SUM(prompt_id IS NOT NULL) prompt_ids, SUM(telegram_sent=1) telegram_sent FROM cycles").fetchone()
    print(json.dumps({"chats": row["chats"], **dict(cycles)}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish Codex final-response usage artifacts")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--data-repo", default=str(DEFAULT_DATA_REPO))
    parser.add_argument("--remote", default=DEFAULT_DATA_REMOTE)
    parser.add_argument("--quota-db", default=str(DEFAULT_QUOTA_DB))
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--no-push", action="store_true")
    run_p.add_argument("--dry-run-telegram", action="store_true")
    run_p.set_defaults(func=command_run)
    sub.add_parser("status").set_defaults(func=command_status)
    return parser


def publisher_error_result(exc: PublisherError) -> int:
    text = str(exc)
    if text == "BLOCKED_REPO_PUBLIC":
        print(json.dumps({"status": text}, sort_keys=True), file=sys.stderr)
        return 76
    print(json.dumps({"status": "error", "error": archive.redact_text(text)}, sort_keys=True), file=sys.stderr)
    return 75


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PublisherError as exc:
        return publisher_error_result(exc)
    except BlockingIOError:
        print(json.dumps({"status": "locked"}, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
