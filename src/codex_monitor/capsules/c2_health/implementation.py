from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_QUOTA_DB = Path.home() / ".local/share/codex-usage-monitor/codex_usage_monitor.db"
DEFAULT_ARCHIVE_DB = Path.home() / ".local/share/codex-session-archive/index/archive.sqlite"
DEFAULT_HISTORY_DB = Path.home() / ".local/share/prompt-history/prompt_history.sqlite"
DEFAULT_ROADMAP_DB = Path.home() / "projects/codex-roadmap/roadmap.sqlite"
DEFAULT_PUBLISHER_STATE = Path.home() / ".local/state/codex-usage-publisher/github-actions-watch.json"
DEFAULT_CREDENTIAL = Path.home() / ".config/codex-usage-monitor/c2-kuma.env"


class C2HealthError(RuntimeError):
    pass


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _age_seconds(value: dt.datetime | None) -> float | None:
    if value is None:
        return None
    return max(0.0, (_utc_now() - value).total_seconds())


def _db(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise C2HealthError(f"missing database: {resolved}")
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _component(ok: bool, *, age: float | None = None, detail: str = "") -> dict[str, Any]:
    return {"ok": bool(ok), "age_seconds": age, "detail": detail}
def _quota_health(path: Path, max_age: float) -> dict[str, Any]:
    try:
        with _db(path) as conn:
            row = conn.execute(
                """SELECT completed_at_utc,status,error FROM acquisition_runs
                     ORDER BY run_id DESC LIMIT 1"""
            ).fetchone()
    except (C2HealthError, sqlite3.Error) as exc:
        return _component(False, detail=str(exc))
    if row is None:
        return _component(False, detail="no acquisition run")
    age = _age_seconds(_parse_iso(row["completed_at_utc"]))
    ok = row["status"] == "ok" and age is not None and age <= max_age
    detail = "ok" if ok else str(row["error"] or row["status"] or "stale")
    return _component(ok, age=age, detail=detail)


def _archive_health(path: Path, max_age: float) -> dict[str, Any]:
    try:
        with _db(path) as conn:
            row = conn.execute(
                """SELECT completed_at_utc,status,error FROM import_runs
                     ORDER BY run_id DESC LIMIT 1"""
            ).fetchone()
    except (C2HealthError, sqlite3.Error) as exc:
        return _component(False, detail=str(exc))
    if row is None:
        return _component(False, detail="no archive import")
    age = _age_seconds(_parse_iso(row["completed_at_utc"]))
    ok = row["status"] == "ok" and age is not None and age <= max_age
    detail = "ok" if ok else str(row["error"] or row["status"] or "stale")
    return _component(ok, age=age, detail=detail)
def _history_health(path: Path, max_age: float) -> dict[str, Any]:
    try:
        stat = path.expanduser().stat()
        age = max(0.0, time.time() - stat.st_mtime)
        with _db(path) as conn:
            prompts = int(conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0])
            sources = {
                str(row[0])
                for row in conn.execute("SELECT DISTINCT source FROM source_records")
            }
    except (OSError, C2HealthError, sqlite3.Error) as exc:
        return _component(False, detail=str(exc))
    required = {"codex-roadmap", "codex-session-bandit", "codex-usage", "chatgpt"}
    missing = sorted(required - sources)
    ok = prompts > 0 and not missing and age <= max_age
    detail = "ok" if ok else (
        f"missing sources: {','.join(missing)}" if missing else f"stale or empty: prompts={prompts}"
    )
    return _component(ok, age=age, detail=detail)


def _roadmap_health(path: Path) -> dict[str, Any]:
    try:
        with _db(path) as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0])
            running = int(
                conn.execute("SELECT COUNT(*) FROM prompts WHERE status='running'").fetchone()[0]
            )
    except (C2HealthError, sqlite3.Error) as exc:
        return _component(False, detail=str(exc))
    return _component(count > 0, detail=f"prompts={count}; running={running}")
def _file_health(path: Path, max_age: float) -> dict[str, Any]:
    try:
        age = max(0.0, time.time() - path.expanduser().stat().st_mtime)
    except OSError as exc:
        return _component(False, detail=str(exc))
    return _component(age <= max_age, age=age, detail="ok" if age <= max_age else "stale")


def aggregate_health(
    *,
    quota_db: Path = DEFAULT_QUOTA_DB,
    archive_db: Path = DEFAULT_ARCHIVE_DB,
    history_db: Path = DEFAULT_HISTORY_DB,
    roadmap_db: Path = DEFAULT_ROADMAP_DB,
    publisher_state: Path = DEFAULT_PUBLISHER_STATE,
) -> dict[str, Any]:
    components = {
        "quota": _quota_health(quota_db, 35 * 60),
        "archive": _archive_health(archive_db, 10 * 60),
        "publisher": _file_health(publisher_state, 10 * 60),
        "history": _history_health(history_db, 35 * 60),
        "roadmap": _roadmap_health(roadmap_db),
    }
    healthy = all(bool(item["ok"]) for item in components.values())
    return {
        "status": "up" if healthy else "down",
        "healthy": healthy,
        "checked_at": _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "components": components,
    }


def _read_env_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None
def load_push_url() -> str | None:
    direct = os.getenv("C2_KUMA_PUSH_URL", "").strip()
    if direct:
        return direct
    credentials_dir = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
    if credentials_dir:
        value = _read_env_value(Path(credentials_dir) / "c2-kuma.env", "C2_KUMA_PUSH_URL")
        if value:
            return value
    return _read_env_value(DEFAULT_CREDENTIAL, "C2_KUMA_PUSH_URL")


def build_push_url(base_url: str, *, status: str, message: str, ping_ms: float | None = None) -> str:
    raw = (base_url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "/api/push/" not in parsed.path:
        raise C2HealthError("invalid Uptime Kuma push URL")
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["status"] = status
    query["msg"] = message[:250]
    if ping_ms is None:
        query.pop("ping", None)
    else:
        query["ping"] = f"{max(0.0, float(ping_ms)):g}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def _health_message(payload: dict[str, Any]) -> str:
    bad = [name for name, item in payload["components"].items() if not item["ok"]]
    if bad:
        return "C2 degraded: " + ", ".join(bad)
    ages = []
    for name, item in payload["components"].items():
        age = item.get("age_seconds")
        if age is not None:
            ages.append(f"{name}={int(age // 60)}m")
    return "C2 healthy" + ("; " + " ".join(ages) if ages else "")
