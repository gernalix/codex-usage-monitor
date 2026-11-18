"""Read-only, near-real-time Codex activity dashboard for Fedora.

Native rollout token counters are NOT provider quota accounting.
No model calls, no auth tokens, and no database writes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

DEFAULT_SESSIONS = Path.home() / ".codex/sessions"
DEFAULT_DB = Path.home() / ".local/share/codex-usage-monitor/codex_usage_monitor.db"
PAGE = Path(__file__).with_name("live_dashboard.html")
TAIL_LIMIT = 4 * 1024 * 1024
SCAN_HORIZON = 300
MAX_FILES = 32


def epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def recent_session(path: Path, now: float, window: int = 60) -> dict[str, Any] | None:
    """Read metadata and bounded rollout tail; never return message contents."""
    try:
        with path.open("rb") as fh:
            header = json.loads(fh.readline(524288))
            meta = header.get("payload", {}) if header.get("type") == "session_meta" else {}
            if not isinstance(meta, dict):
                meta = {}
            first_at = epoch(header.get("timestamp"))
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            offset = max(0, size - TAIL_LIMIT)
            fh.seek(offset)
            if offset:
                fh.readline()  # discard incomplete JSONL record
            records: list[tuple[float, int]] = []
            for raw in fh:
                if len(raw) > 1024 * 1024:
                    continue
                try:
                    item = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if item.get("type") != "event_msg":
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                totals = info.get("total_token_usage") if isinstance(info, dict) else None
                when = epoch(item.get("timestamp"))
                if not isinstance(totals, dict) or when is None:
                    continue
                total = totals.get("total_tokens")
                if isinstance(total, int) and total >= 0:
                    records.append((when, total))
    except (OSError, ValueError, UnicodeError):
        return None

    if not records:
        return None
    latest_at = records[-1][0]
    if latest_at < now - SCAN_HORIZON:
        return None
    # A delta is counted only between two consecutive cumulative counters.
    # For a brand-new session, the first counter can be compared to zero.
    start = now - window
    observed = 0
    events = 0
    missing_baseline = False
    previous: int | None = None
    for when, current in records:
        if when >= start:
            events += 1
            if previous is not None and current >= previous:
                observed += current - previous
            elif previous is None and first_at is not None and first_at >= start:
                observed += current
            elif previous is None:
                missing_baseline = True
        previous = current

    cwd = meta.get("cwd")
    project = Path(cwd).name[:80] if isinstance(cwd, str) and cwd else "sconosciuto"
    session_id = meta.get("id") or path.stem[-36:]
    source = meta.get("originator") or meta.get("source") or "non specificata"
    return {
        "session_id": str(session_id)[:90],
        "project": project,
        "source": str(source)[:60],
        "last_token_at": utc(latest_at),
        "seconds_since_tokens": max(0, int(now - latest_at)),
        "tokens_60s": observed,
        "token_events_60s": events,
        "partial": missing_baseline or offset > 0,
        "thread_url": "codex://threads/" + str(session_id)[:90],
    }


def recent_sessions(root: Path = DEFAULT_SESSIONS, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    if not root.is_dir():
        return {"status": "unavailable", "sessions": [], "scanned": 0, "truncated": False}
    paths = []
    try:
        for path in root.rglob("rollout-*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= now - SCAN_HORIZON:
                paths.append((stat.st_mtime, path))
    except OSError:
        return {"status": "unavailable", "sessions": [], "scanned": 0, "truncated": False}
    paths.sort(reverse=True)
    truncated = len(paths) > MAX_FILES
    sessions = [result for _, path in paths[:MAX_FILES]
                if (result := recent_session(path, now)) is not None]
    sessions.sort(key=lambda r: (r["tokens_60s"], -r["seconds_since_tokens"]), reverse=True)
    return {"status": "observed" if sessions else "no_recent_events",
            "sessions": sessions, "scanned": min(len(paths), MAX_FILES), "truncated": truncated}


def codex_processes(proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    """Report plausible local hosts, not invented session-to-PID attribution."""
    results = []
    try:
        candidates = proc_root.iterdir()
        for item in candidates:
            if not item.name.isdigit():
                continue
            try:
                raw = (item / "cmdline").read_bytes()[:12288]
            except (OSError, PermissionError):
                continue
            args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
            if not args:
                continue
            exe = Path(args[0]).name
            role = None
            if exe == "ChatGPT" and not any(a.startswith("--type=") for a in args):
                role = "Desktop (interfaccia)"
            elif exe == "codex":
                if "app-server" in args:
                    role = "Desktop (app-server)"
                elif "exec-server" in args:
                    role = "Desktop (esecuzioni cloud)"
                else:
                    role = "CLI / agente Codex"
            elif any(Path(arg).name == "codex_human_log.py" for arg in args[1:]):
                role = "Traduttore AI del log"
            if role:
                results.append({"pid": int(item.name), "role": role,
                                "note": "Processo attivo: consumo non dimostrato dal solo PID"})
    except OSError:
        return []
    return sorted(results, key=lambda p: (p["role"], p["pid"]))


def quota_snapshot(db: Path = DEFAULT_DB) -> dict[str, Any]:
    if not db.is_file():
        return {"status": "unavailable"}
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """SELECT snapshot_id, acquired_at_utc, weekly_used_percent,
                          weekly_remaining_percent, weekly_reset_at_utc
                   FROM quota_snapshots WHERE acquisition_status='ok'
                   ORDER BY snapshot_id DESC LIMIT 200"""
            ).fetchall()
            if not rows:
                return {"status": "empty"}
            latest = rows[0]
            last_drop = None
            for newer, older in zip(rows, rows[1:]):
                if (newer["weekly_used_percent"] is not None
                    and older["weekly_used_percent"] is not None
                    and newer["weekly_reset_at_utc"] == older["weekly_reset_at_utc"]
                    and newer["weekly_used_percent"] > older["weekly_used_percent"]):
                    last_drop = {"at": newer["acquired_at_utc"],
                                 "delta_pp": round(newer["weekly_used_percent"] - older["weekly_used_percent"], 2)}
                    report = con.execute(
                        "SELECT diagnostic_json FROM quota_attributions WHERE snapshot_id=?",
                        (newer["snapshot_id"],),
                    ).fetchone()
                    if report:
                        data = json.loads(report["diagnostic_json"])
                        last_drop["local_sessions"] = len(data.get("sessions", []))
                        last_drop["observed_tokens"] = data.get("observed_total_tokens", 0)
                    break
            return {"status": "ok", "acquired_at": latest["acquired_at_utc"],
                    "used_percent": latest["weekly_used_percent"],
                    "remaining_percent": latest["weekly_remaining_percent"],
                    "resets_at": latest["weekly_reset_at_utc"], "last_drop": last_drop}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return {"status": "error"}


def snapshot(sessions_root: Path = DEFAULT_SESSIONS, db: Path = DEFAULT_DB,
             proc_root: Path = Path("/proc"), now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    return {"observed_at": utc(now), "quota": quota_snapshot(db),
            "local": recent_sessions(sessions_root, now),
            "processes": codex_processes(proc_root),
            "caveat": "Token dei JSONL locali: non rappresentano la quota OpenAI per PID. Sessioni cloud o altri dispositivi possono non comparire."}


class Handler(BaseHTTPRequestHandler):
    cache_lock = threading.Lock()
    cache_at = 0.0
    cache_data: dict[str, Any] | None = None

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            data = PAGE.read_bytes()
            self.send_data(data, "text/html; charset=utf-8")
        elif path == "/api/status":
            cls = type(self)
            with cls.cache_lock:
                if cls.cache_data is None or time.monotonic() - cls.cache_at > 1.5:
                    cls.cache_data = snapshot()
                    cls.cache_at = time.monotonic()
                data = json.dumps(cls.cache_data, ensure_ascii=False).encode("utf-8")
            self.send_data(data, "application/json; charset=utf-8")
        elif path == "/healthz":
            self.send_data(b'{"status":"ok"}', "application/json")
        else:
            self.send_error(404)

    def send_data(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass  # avoid exposing thread identifiers in journald


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
