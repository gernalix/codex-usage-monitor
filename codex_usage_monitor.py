#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Any


VERSION = "2026.08.02"
APP_NAME = "codex-usage-monitor"
DEFAULT_DB = Path("/home/ubuntu/sync_root/db/codex_usage_monitor.db")
DEFAULT_STATE_DIR = Path("/home/ubuntu/.local/state/codex-usage-monitor")
DEFAULT_TELEGRAM_HELPER = Path("/home/ubuntu/telegram_notify.py")
DEFAULT_CODEX_BIN = "/usr/bin/codex"
DEFAULT_APP_SERVER_PORT = 38655
SOURCE_METHOD = "codex-app-server account/rateLimits/read"


class MonitorError(RuntimeError):
    pass


class SourceError(MonitorError):
    pass


class ConfigError(MonitorError):
    pass


SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(api\.telegram\.org/bot)[^/\s]+"), r"\1[REDACTED]"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "[TELEGRAM_TOKEN_REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[OPENAI_KEY_REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"), "[JWT_REDACTED]"),
    (re.compile(r'("(?:accessToken|sessionToken|idToken|refreshToken|token)"\s*:\s*")[^"]+(")', re.I), r"\1[REDACTED]\2"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"), "[EMAIL_REDACTED]"),
    (re.compile(r"user-[A-Za-z0-9]+"), "user-REDACTED"),
)


@dataclass(frozen=True)
class Config:
    db_path: Path
    state_dir: Path
    lock_path: Path
    codex_bin: str
    app_server_port: int
    source_timeout_sec: int
    startup_timeout_sec: int
    sqlite_timeout_sec: int
    telegram_helper: Path
    telegram_enabled: bool
    notify_approaching_expiry_hours: int
    notify_failure_after_runs: int
    notification_cooldown_minutes: int


@dataclass(frozen=True)
class QuotaReading:
    weekly_used_percent: float | None
    weekly_remaining_percent: float | None
    weekly_reset_at_utc: str | None
    usage_limit_resets_available: int | None
    source_format: str
    source_payload_sha256: str | None
    sanitized_excerpt: str
    parse_warnings: tuple[str, ...]
    payload: dict[str, Any] | None


class ExclusiveLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "ExclusiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MonitorError(f"another {APP_NAME} execution already holds {self.path}") from exc
        self.handle.write(f"{os.getpid()}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_stamp(value: dt.datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def getenv_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def getenv_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = int(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def build_config(args: argparse.Namespace | None = None) -> Config:
    env_file = Path(os.getenv("CODEX_USAGE_ENV_FILE", "/home/ubuntu/.config/codex-usage-monitor/codex-usage-monitor.env"))
    load_env_file(env_file.expanduser())
    db_path = Path(os.getenv("CODEX_USAGE_DB", str(DEFAULT_DB))).expanduser()
    state_dir = Path(os.getenv("CODEX_USAGE_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()
    return Config(
        db_path=db_path,
        state_dir=state_dir,
        lock_path=Path(os.getenv("CODEX_USAGE_LOCK", str(state_dir / "codex-usage-monitor.lock"))).expanduser(),
        codex_bin=os.getenv("CODEX_USAGE_CODEX_BIN", os.getenv("CODEX_CLI_BIN", DEFAULT_CODEX_BIN)),
        app_server_port=getenv_int("CODEX_USAGE_APP_SERVER_PORT", DEFAULT_APP_SERVER_PORT, 1024),
        source_timeout_sec=getenv_int("CODEX_USAGE_SOURCE_TIMEOUT_SEC", 45, 5),
        startup_timeout_sec=getenv_int("CODEX_USAGE_STARTUP_TIMEOUT_SEC", 90, 5),
        sqlite_timeout_sec=getenv_int("CODEX_USAGE_SQLITE_TIMEOUT_SEC", 30, 1),
        telegram_helper=Path(os.getenv("CODEX_USAGE_TELEGRAM_HELPER", str(DEFAULT_TELEGRAM_HELPER))).expanduser(),
        telegram_enabled=getenv_bool("CODEX_USAGE_TELEGRAM_ENABLED", True),
        notify_approaching_expiry_hours=getenv_int("CODEX_USAGE_NOTIFY_EXPIRY_HOURS", 12, 1),
        notify_failure_after_runs=getenv_int("CODEX_USAGE_NOTIFY_FAILURE_AFTER_RUNS", 3, 1),
        notification_cooldown_minutes=getenv_int("CODEX_USAGE_NOTIFICATION_COOLDOWN_MINUTES", 60, 1),
    )


def sanitize(text: Any, limit: int = 1000) -> str:
    if not isinstance(text, str):
        if isinstance(text, BaseException):
            text = str(text)
        else:
            try:
                text = json.dumps(text, ensure_ascii=False, sort_keys=True, default=str)
            except TypeError:
                text = str(text)
    redacted = str(text).replace("\x00", " ")
    for pattern, replacement in SENSITIVE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    collapsed = " ".join(redacted.split())
    return collapsed if len(collapsed) <= limit else collapsed[: max(0, limit - 3)] + "..."


def parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def clamp_percent(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 3)


def epoch_to_utc_iso(value: Any) -> str | None:
    number = parse_float(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        number /= 1000.0
    try:
        return utc_stamp(dt.datetime.fromtimestamp(number, tz=dt.timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def http_ready(port: int, timeout: float = 2.0) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


class AppServerManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.process: subprocess.Popen[str] | None = None

    def ensure_ready(self) -> None:
        if http_ready(self.cfg.app_server_port):
            return
        codex_bin = shutil.which(self.cfg.codex_bin) or self.cfg.codex_bin
        args = [codex_bin, "app-server", "--listen", f"ws://127.0.0.1:{self.cfg.app_server_port}"]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        deadline = time.monotonic() + self.cfg.startup_timeout_sec
        while time.monotonic() < deadline:
            if http_ready(self.cfg.app_server_port):
                return
            if self.process.poll() is not None:
                raise SourceError(f"Codex app-server exited with status {self.process.returncode}")
            time.sleep(0.5)
        raise SourceError(f"Codex app-server did not become ready on port {self.cfg.app_server_port}")

    def stop(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class WebSocketClient:
    def __init__(self, host: str, port: int, timeout: float):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def __enter__(self) -> "WebSocketClient":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                break
            header += chunk
            if len(header) > 65536:
                break
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise SourceError(f"WebSocket handshake failed: {sanitize(header.decode('latin-1', 'replace'), 300)}")
        self.sock = sock
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def send_text(self, text: str) -> None:
        if self.sock is None:
            raise SourceError("WebSocket is not connected")
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = secrets.token_bytes(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def receive_json(self) -> dict[str, Any]:
        text = self.receive_text()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceError(f"WebSocket returned invalid JSON: {sanitize(text, 300)}") from exc
        if not isinstance(decoded, dict):
            raise SourceError("WebSocket returned non-object JSON")
        return decoded

    def receive_text(self) -> str:
        if self.sock is None:
            raise SourceError("WebSocket is not connected")
        chunks: list[bytes] = []
        while True:
            first = self._recv_exact(2)
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise SourceError("WebSocket closed by server")
            if opcode == 0x9:
                continue
            if opcode in (0x1, 0x0):
                chunks.append(payload)
                if first[0] & 0x80:
                    return b"".join(chunks).decode("utf-8", "replace")

    def _recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise SourceError("WebSocket is not connected")
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise SourceError("WebSocket connection closed unexpectedly")
            data += chunk
        return data


def extract_json_object_after(text: str, marker: str) -> dict[str, Any] | None:
    start = text.find(marker)
    if start < 0:
        return None
    brace_start = text.find("{", start + len(marker))
    if brace_start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(brace_start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    decoded = json.loads(text[brace_start : index + 1])
                except json.JSONDecodeError:
                    return None
                return decoded if isinstance(decoded, dict) else None
    return None


def normalize_rate_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used = value.get("usedPercent", value.get("used_percent"))
    remaining = value.get("remainingPercent", value.get("remaining_percent"))
    duration = value.get("windowDurationMins", value.get("window_duration_mins"))
    if duration is None:
        seconds = parse_float(value.get("limit_window_seconds"))
        duration = int(round(seconds / 60.0)) if seconds is not None else None
    return {
        "usedPercent": used,
        "remainingPercent": remaining,
        "windowDurationMins": duration,
        "resetsAt": value.get("resetsAt", value.get("reset_at")),
    }


def normalize_wham_usage_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    if "rateLimits" in payload:
        return payload
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    main = {
        "limitId": "codex",
        "limitName": None,
        "primary": normalize_rate_window(rate_limit.get("primary_window")),
        "secondary": normalize_rate_window(rate_limit.get("secondary_window")),
        "credits": payload.get("credits"),
        "individualLimit": (payload.get("spend_control") or {}).get("individual_limit") if isinstance(payload.get("spend_control"), dict) else None,
        "planType": payload.get("plan_type"),
        "rateLimitReachedType": payload.get("rate_limit_reached_type"),
    }
    return {"rateLimits": main, "rateLimitsByLimitId": {"codex": main}, "_source": "codex-app-server-wham-fallback"}


def jsonrpc_call(ws: WebSocketClient, request_id: int, method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    ws.send_json(message)
    deadline = time.monotonic() + ws.timeout
    while time.monotonic() < deadline:
        response = ws.receive_json()
        if response.get("id") != request_id:
            continue
        if "error" in response:
            error = response.get("error")
            text = error.get("message") if isinstance(error, dict) else str(error)
            if method == "account/rateLimits/read" and isinstance(text, str):
                body = extract_json_object_after(text, "body=")
                if body is not None:
                    normalized = normalize_wham_usage_payload(body)
                    if normalized is not None:
                        return normalized
            raise SourceError(f"JSON-RPC {method} error: {sanitize(error, 600)}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise SourceError(f"JSON-RPC {method} returned invalid result")
        return result
    raise SourceError(f"Timed out waiting for JSON-RPC response to {method}")


def read_rate_limits(cfg: Config) -> dict[str, Any]:
    manager = AppServerManager(cfg)
    try:
        manager.ensure_ready()
        with WebSocketClient("127.0.0.1", cfg.app_server_port, timeout=cfg.source_timeout_sec) as ws:
            jsonrpc_call(
                ws,
                1,
                "initialize",
                {"clientInfo": {"name": APP_NAME, "title": "Codex Usage Monitor", "version": VERSION}},
            )
            return jsonrpc_call(ws, 2, "account/rateLimits/read")
    finally:
        manager.stop()


def iter_limit_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []

    def add(limit_id: str, limit_name: Any, window_name: str, raw: Any, source_path: str) -> None:
        if not isinstance(raw, dict):
            return
        normalized = normalize_rate_window(raw)
        if not normalized:
            return
        windows.append(
            {
                "limit_id": limit_id,
                "limit_name": limit_name,
                "window_name": window_name,
                "source_path": source_path,
                **normalized,
            }
        )

    result = payload.get("rateLimits") if isinstance(payload.get("rateLimits"), dict) else payload
    if isinstance(result, dict):
        limit_id = str(result.get("limitId") or "codex")
        limit_name = result.get("limitName")
        add(limit_id, limit_name, "primary", result.get("primary"), "rateLimits.primary")
        add(limit_id, limit_name, "secondary", result.get("secondary"), "rateLimits.secondary")
    by_limit = payload.get("rateLimitsByLimitId")
    if isinstance(by_limit, dict):
        for raw_id, item in by_limit.items():
            if not isinstance(item, dict):
                continue
            limit_id = str(item.get("limitId") or raw_id or "")
            limit_name = item.get("limitName")
            add(limit_id, limit_name, "primary", item.get("primary"), f"rateLimitsByLimitId.{limit_id}.primary")
            add(limit_id, limit_name, "secondary", item.get("secondary"), f"rateLimitsByLimitId.{limit_id}.secondary")
    return windows


def choose_weekly_window(payload: dict[str, Any]) -> tuple[float | None, float | None, str | None, list[str]]:
    warnings: list[str] = []
    candidates = []
    for window in iter_limit_windows(payload):
        duration_raw = window.get("windowDurationMins")
        try:
            duration = int(duration_raw) if duration_raw is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration == 10080:
            candidates.append(window)
    if not candidates:
        warnings.append("weekly window with windowDurationMins=10080 not found")
        return None, None, None, warnings
    selected = candidates[0]
    used = parse_float(selected.get("usedPercent"))
    remaining = parse_float(selected.get("remainingPercent"))
    if used is None and remaining is not None:
        used = clamp_percent(100.0 - remaining)
    if remaining is None and used is not None:
        remaining = clamp_percent(100.0 - used)
    reset = epoch_to_utc_iso(selected.get("resetsAt"))
    if used is None:
        warnings.append("weekly used percent unavailable")
    if reset is None:
        warnings.append("weekly reset timestamp unavailable")
    return clamp_percent(used) if used is not None else None, clamp_percent(remaining) if remaining is not None else None, reset, warnings


def parse_reset_count_from_text(text: str) -> int | None:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if re.search(r"\b(no|zero|0)\s+(?:free\s+)?(?:usage\s+limit\s+|rate\s+limit\s+)?resets?\s+(?:available|remaining|left)\b", lowered):
        return 0
    patterns = (
        r"\b(\d+)\s+(?:free\s+)?(?:usage\s+limit\s+|rate\s+limit\s+)?resets?\s+(?:available|remaining|left)\b",
        r"\b(?:usage\s+limit\s+|rate\s+limit\s+)?resets?\s+(?:available|remaining|left)\s*[:=]\s*(\d+)\b",
        r"\bavailable\s+(?:usage\s+limit\s+|rate\s+limit\s+)?resets?\s*[:=]?\s*(\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    if re.search(r"\b(one|a|an)\s+(?:free\s+)?(?:usage\s+limit\s+|rate\s+limit\s+)?reset\s+(?:available|remaining|left)\b", lowered):
        return 1
    return None


def reset_count_from_structured(payload: dict[str, Any]) -> tuple[int | None, list[str]]:
    warnings: list[str] = []
    root = payload.get("rateLimitResetCredits")
    if not isinstance(root, dict):
        root = payload.get("usageLimitResetCredits")
    if not isinstance(root, dict):
        return None, ["structured reset credit object missing"]
    available = root.get("availableCount", root.get("available_count"))
    if available is not None and not isinstance(available, bool):
        try:
            parsed = int(available)
            return parsed if parsed >= 0 else None, warnings
        except (TypeError, ValueError):
            warnings.append("structured availableCount malformed")
    credits = root.get("credits")
    if isinstance(credits, list):
        count = 0
        for item in credits:
            if isinstance(item, dict) and str(item.get("status", "")).lower() == "available":
                count += 1
        return count, warnings
    warnings.append("structured reset credit list missing")
    return None, warnings


def reset_count_from_payload(payload: dict[str, Any]) -> tuple[int | None, list[str]]:
    structured, warnings = reset_count_from_structured(payload)
    if structured is not None:
        return structured, warnings
    for key in ("message", "description", "summary", "raw", "text"):
        value = payload.get(key)
        if isinstance(value, str):
            parsed = parse_reset_count_from_text(value)
            if parsed is not None:
                warnings.append(f"reset count parsed from text field {key}")
                return parsed, warnings
    return None, warnings


def reading_from_payload(payload: dict[str, Any]) -> QuotaReading:
    used, remaining, reset, quota_warnings = choose_weekly_window(payload)
    reset_count, reset_warnings = reset_count_from_payload(payload)
    digest = payload_hash(payload)
    source_format = str(payload.get("_source") or payload.get("source") or "codex-app-server-jsonrpc-v1")
    return QuotaReading(
        weekly_used_percent=used,
        weekly_remaining_percent=remaining,
        weekly_reset_at_utc=reset,
        usage_limit_resets_available=reset_count,
        source_format=source_format,
        source_payload_sha256=digest,
        sanitized_excerpt=sanitize(payload),
        parse_warnings=tuple(quota_warnings + reset_warnings),
        payload=payload,
    )


def connect_db(cfg: Config) -> sqlite3.Connection:
    import sqlite3

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg.db_path, timeout=cfg.sqlite_timeout_sec)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={cfg.sqlite_timeout_sec * 1000}")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(cfg: Config) -> None:
    with connect_db(cfg) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS acquisition_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                status TEXT NOT NULL,
                source_method TEXT NOT NULL,
                source_version TEXT NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS quota_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES acquisition_runs(run_id),
                acquired_at_utc TEXT NOT NULL,
                acquired_minute_utc TEXT NOT NULL,
                acquisition_status TEXT NOT NULL,
                source_method TEXT NOT NULL,
                source_version TEXT NOT NULL,
                source_format TEXT,
                weekly_used_percent REAL,
                weekly_remaining_percent REAL,
                weekly_reset_at_utc TEXT,
                usage_limit_resets_available INTEGER,
                sanitized_error TEXT,
                sanitized_excerpt TEXT,
                source_payload_sha256 TEXT,
                parse_warnings TEXT,
                provenance TEXT NOT NULL DEFAULT 'live',
                created_at_utc TEXT NOT NULL,
                UNIQUE(acquired_minute_utc, source_payload_sha256, acquisition_status, provenance)
            );
            CREATE INDEX IF NOT EXISTS idx_quota_snapshots_acquired ON quota_snapshots(acquired_at_utc);
            CREATE INDEX IF NOT EXISTS idx_quota_snapshots_status ON quota_snapshots(acquisition_status, acquired_at_utc);

            CREATE TABLE IF NOT EXISTS notification_events (
                notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER REFERENCES quota_snapshots(snapshot_id),
                event_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                decision TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0 CHECK(sent IN (0,1)),
                detail TEXT,
                created_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notification_events_key_created ON notification_events(event_key, created_at_utc);

            CREATE TABLE IF NOT EXISTS imports (
                import_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                imported_at_utc TEXT NOT NULL,
                rows_imported INTEGER NOT NULL,
                source_sha256 TEXT,
                backup_path TEXT
            );

            CREATE VIEW IF NOT EXISTS latest_state AS
            SELECT *
            FROM quota_snapshots
            ORDER BY acquired_at_utc DESC, snapshot_id DESC
            LIMIT 1;

            CREATE VIEW IF NOT EXISTS history AS
            SELECT snapshot_id, acquired_at_utc, acquisition_status, weekly_used_percent,
                   weekly_remaining_percent, weekly_reset_at_utc,
                   usage_limit_resets_available, source_format, provenance,
                   sanitized_error, parse_warnings
            FROM quota_snapshots
            ORDER BY acquired_at_utc DESC, snapshot_id DESC;

            CREATE VIEW IF NOT EXISTS reset_count_changes AS
            WITH ordered AS (
                SELECT snapshot_id, acquired_at_utc, usage_limit_resets_available,
                       LAG(usage_limit_resets_available) OVER (ORDER BY acquired_at_utc, snapshot_id) AS previous_resets
                FROM quota_snapshots
                WHERE acquisition_status = 'ok'
            )
            SELECT *
            FROM ordered
            WHERE usage_limit_resets_available IS NOT previous_resets
            ORDER BY acquired_at_utc DESC, snapshot_id DESC;

            CREATE VIEW IF NOT EXISTS recent_failures AS
            SELECT snapshot_id, acquired_at_utc, acquisition_status, sanitized_error, parse_warnings, source_format
            FROM quota_snapshots
            WHERE acquisition_status != 'ok'
            ORDER BY acquired_at_utc DESC, snapshot_id DESC
            LIMIT 50;
            """
        )
        con.commit()


def start_run(con: sqlite3.Connection) -> int:
    now = utc_stamp()
    cur = con.execute(
        "INSERT INTO acquisition_runs (started_at_utc,status,source_method,source_version) VALUES (?,?,?,?)",
        (now, "running", SOURCE_METHOD, VERSION),
    )
    con.commit()
    return int(cur.lastrowid)


def finish_run(con: sqlite3.Connection, run_id: int, status: str, error: str = "") -> None:
    con.execute(
        "UPDATE acquisition_runs SET completed_at_utc=?, status=?, error=? WHERE run_id=?",
        (utc_stamp(), status, error, run_id),
    )
    con.commit()


def insert_snapshot(
    con: sqlite3.Connection,
    run_id: int,
    status: str,
    reading: QuotaReading | None,
    error: str = "",
    acquired_at: dt.datetime | None = None,
    provenance: str = "live",
) -> int:
    acquired = acquired_at or utc_now()
    acquired_text = utc_stamp(acquired)
    minute_text = acquired.astimezone(dt.timezone.utc).replace(second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    digest = reading.source_payload_sha256 if reading else hashlib.sha256(f"{acquired_text}:{status}:{error}".encode()).hexdigest()
    values = {
        "run_id": run_id,
        "acquired_at_utc": acquired_text,
        "acquired_minute_utc": minute_text,
        "acquisition_status": status,
        "source_method": SOURCE_METHOD,
        "source_version": VERSION,
        "source_format": reading.source_format if reading else None,
        "weekly_used_percent": reading.weekly_used_percent if reading else None,
        "weekly_remaining_percent": reading.weekly_remaining_percent if reading else None,
        "weekly_reset_at_utc": reading.weekly_reset_at_utc if reading else None,
        "usage_limit_resets_available": reading.usage_limit_resets_available if reading else None,
        "sanitized_error": sanitize(error, 700) if error else None,
        "sanitized_excerpt": reading.sanitized_excerpt if reading else None,
        "source_payload_sha256": digest,
        "parse_warnings": "; ".join(reading.parse_warnings) if reading and reading.parse_warnings else None,
        "provenance": provenance,
        "created_at_utc": utc_stamp(),
    }
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    try:
        cur = con.execute(f"INSERT INTO quota_snapshots ({columns}) VALUES ({placeholders})", list(values.values()))
        con.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        row = con.execute(
            """
            SELECT snapshot_id FROM quota_snapshots
            WHERE acquired_minute_utc=? AND source_payload_sha256=? AND acquisition_status=? AND provenance=?
            ORDER BY snapshot_id DESC LIMIT 1
            """,
            (minute_text, digest, status, provenance),
        ).fetchone()
        return int(row["snapshot_id"]) if row else 0


def latest_ok_before(con: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT * FROM quota_snapshots
        WHERE acquisition_status='ok' AND snapshot_id < ?
        ORDER BY acquired_at_utc DESC, snapshot_id DESC LIMIT 1
        """,
        (snapshot_id,),
    ).fetchone()


def consecutive_failures(con: sqlite3.Connection) -> int:
    rows = con.execute(
        "SELECT acquisition_status FROM quota_snapshots ORDER BY acquired_at_utc DESC, snapshot_id DESC LIMIT 50"
    ).fetchall()
    total = 0
    for row in rows:
        if row["acquisition_status"] == "ok":
            break
        total += 1
    return total


def notification_recently_sent(con: sqlite3.Connection, event_key: str, cooldown_minutes: int) -> bool:
    cutoff = utc_now() - dt.timedelta(minutes=cooldown_minutes)
    rows = con.execute(
        "SELECT created_at_utc FROM notification_events WHERE event_key=? AND sent=1 ORDER BY created_at_utc DESC LIMIT 1",
        (event_key,),
    ).fetchall()
    for row in rows:
        try:
            ts = dt.datetime.fromisoformat(row["created_at_utc"].replace("Z", "+00:00"))
        except ValueError:
            continue
        return ts >= cutoff
    return False


def build_notification_events(cfg: Config, con: sqlite3.Connection, snapshot_id: int) -> list[tuple[str, str, str, str]]:
    current = con.execute("SELECT * FROM quota_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if current is None:
        return []
    events: list[tuple[str, str, str, str]] = []
    reset_count_text = "unavailable" if current["usage_limit_resets_available"] is None else str(current["usage_limit_resets_available"])
    if current["acquisition_status"] != "ok":
        failures = consecutive_failures(con)
        if failures >= cfg.notify_failure_after_runs:
            events.append(
                (
                    "persistent_failure",
                    "persistent_failure",
                    "Codex usage monitor persistent failure",
                    f"Codex usage acquisition failed {failures} consecutive times. Error: {current['sanitized_error'] or 'unknown'}",
                )
            )
        return events
    previous = latest_ok_before(con, snapshot_id)
    if previous:
        if previous["weekly_used_percent"] != current["weekly_used_percent"] or previous["weekly_reset_at_utc"] != current["weekly_reset_at_utc"]:
            events.append(
                (
                    f"quota_change:{current['weekly_used_percent']}:{current['weekly_reset_at_utc']}",
                    "quota_change",
                    "Codex weekly quota changed",
                    f"Weekly used {previous['weekly_used_percent']} -> {current['weekly_used_percent']} percent; weekly reset {current['weekly_reset_at_utc'] or 'unavailable'}; usage limit resets available {reset_count_text}",
                )
            )
        if previous["usage_limit_resets_available"] != current["usage_limit_resets_available"]:
            events.append(
                (
                    f"reset_count_change:{current['usage_limit_resets_available']}",
                    "reset_count_change",
                    "Codex usage reset count changed",
                    f"Usage limit resets available {previous['usage_limit_resets_available']} -> {current['usage_limit_resets_available']}",
                )
            )
    reset_raw = current["weekly_reset_at_utc"]
    if reset_raw:
        try:
            reset_at = dt.datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
            hours = (reset_at - utc_now()).total_seconds() / 3600.0
            if 0 <= hours <= cfg.notify_approaching_expiry_hours:
                events.append(
                    (
                        f"approaching_expiry:{reset_at.strftime('%Y%m%dT%H')}",
                        "approaching_expiry",
                        "Codex weekly quota reset approaching",
                        f"Weekly quota reset is in {hours:.1f} hours at {reset_raw}; usage limit resets available {reset_count_text}.",
                    )
                )
        except ValueError:
            pass
    return events


def load_telegram_helper(path: Path) -> ModuleType:
    if not path.exists():
        raise ConfigError(f"Telegram helper not found: {path}")
    spec = importlib.util.spec_from_file_location("telegram_notify", path)
    if not spec or not spec.loader:
        raise ConfigError(f"Telegram helper cannot be imported: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def send_telegram(cfg: Config, title: str, message: str) -> str:
    module = load_telegram_helper(cfg.telegram_helper)
    try:
        if hasattr(module, "send_message"):
            module.send_message(title, message)
        elif hasattr(module, "notify"):
            module.notify(message, title=title)
        else:
            raise ConfigError("Telegram helper lacks send_message/notify")
    except SystemExit as exc:
        raise RuntimeError(f"Telegram helper exited with code {exc.code}") from exc
    return f"sent via {cfg.telegram_helper}"


def dispatch_notifications(cfg: Config, con: sqlite3.Connection, snapshot_id: int, dry_run: bool = False) -> None:
    if not cfg.telegram_enabled:
        return
    for event_key, event_type, title, message in build_notification_events(cfg, con, snapshot_id):
        if notification_recently_sent(con, event_key, cfg.notification_cooldown_minutes):
            con.execute(
                """
                INSERT INTO notification_events
                (snapshot_id,event_key,event_type,title,message,decision,sent,detail,created_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (snapshot_id, event_key, event_type, title, message, "deduped", 0, "cooldown", utc_stamp()),
            )
            con.commit()
            continue
        sent = False
        detail = "dry_run"
        decision = "dry_run"
        if not dry_run:
            try:
                detail = send_telegram(cfg, title, message)
                sent = True
                decision = "sent"
            except Exception as exc:
                detail = sanitize(exc, 700)
                decision = "failed"
        con.execute(
            """
            INSERT INTO notification_events
            (snapshot_id,event_key,event_type,title,message,decision,sent,detail,created_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (snapshot_id, event_key, event_type, title, message, decision, 1 if sent else 0, detail, utc_stamp()),
        )
        con.commit()


def command_once(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    init_db(cfg)
    with ExclusiveLock(cfg.lock_path), connect_db(cfg) as con:
        run_id = start_run(con)
        try:
            payload = read_rate_limits(cfg)
            reading = reading_from_payload(payload)
            status = "ok" if reading.weekly_used_percent is not None else "partial"
            snapshot_id = insert_snapshot(con, run_id, status, reading)
            finish_run(con, run_id, status)
            dispatch_notifications(cfg, con, snapshot_id, dry_run=args.dry_run_notifications)
            print(json.dumps({"snapshot_id": snapshot_id, "status": status, "weekly_used_percent": reading.weekly_used_percent, "weekly_reset_at_utc": reading.weekly_reset_at_utc, "usage_limit_resets_available": reading.usage_limit_resets_available}, sort_keys=True))
            return 0 if status == "ok" else 1
        except Exception as exc:
            error = sanitize(exc, 700)
            snapshot_id = insert_snapshot(con, run_id, "error", None, error=error)
            finish_run(con, run_id, "error", error)
            dispatch_notifications(cfg, con, snapshot_id, dry_run=args.dry_run_notifications)
            print(json.dumps({"snapshot_id": snapshot_id, "status": "error", "error": error}, sort_keys=True), file=sys.stderr)
            return 1


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_legacy_ts(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def migrate_legacy_csv(cfg: Config, source_path: Path, backup_path: str = "") -> int:
    if not source_path.exists():
        return 0
    imported = 0
    with connect_db(cfg) as con:
        already = con.execute("SELECT 1 FROM imports WHERE source_path=? LIMIT 1", (str(source_path),)).fetchone()
        if already:
            return 0
        run_id = start_run(con)
        with source_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            for row in csv.DictReader(handle):
                acquired = parse_legacy_ts(row.get("logged_at_local", "")) or utc_now()
                used = parse_float(row.get("weekly_used_pct"))
                remaining = parse_float(row.get("weekly_percent_left"))
                reset = row.get("weekly_reset_at") or None
                if reset:
                    parsed = parse_legacy_ts(reset)
                    reset = utc_stamp(parsed) if parsed else reset
                reading = QuotaReading(
                    weekly_used_percent=used,
                    weekly_remaining_percent=remaining,
                    weekly_reset_at_utc=reset,
                    usage_limit_resets_available=None,
                    source_format="legacy-codex-weekly-limit-monitor-csv",
                    source_payload_sha256=hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
                    sanitized_excerpt=sanitize(row),
                    parse_warnings=(),
                    payload=None,
                )
                insert_snapshot(con, run_id, "ok", reading, acquired_at=acquired, provenance="legacy_csv")
                imported += 1
        finish_run(con, run_id, "ok")
        con.execute(
            "INSERT INTO imports (source_path,source_kind,imported_at_utc,rows_imported,source_sha256,backup_path) VALUES (?,?,?,?,?,?)",
            (str(source_path), "legacy_csv", utc_stamp(), imported, file_sha256(source_path), backup_path),
        )
        con.commit()
    return imported


def command_migrate(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    init_db(cfg)
    total = migrate_legacy_csv(cfg, Path(args.legacy_csv), args.backup_path or "")
    print(json.dumps({"legacy_csv_imported": total}, sort_keys=True))
    return 0


def command_status(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    init_db(cfg)
    with connect_db(cfg) as con:
        row = con.execute("SELECT * FROM latest_state").fetchone()
        if row is None:
            print("Codex usage monitor: no snapshots")
            return 1
        print(json.dumps(dict(row), sort_keys=True))
    return 0


def command_notify_test(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    init_db(cfg)
    with connect_db(cfg) as con:
        latest = con.execute("SELECT * FROM latest_state").fetchone()
    if latest is None:
        snapshot_text = "No SQLite snapshot is available yet."
    else:
        reset_count_text = "unavailable" if latest["usage_limit_resets_available"] is None else str(latest["usage_limit_resets_available"])
        snapshot_text = (
            f"Latest snapshot {latest['snapshot_id']} at {latest['acquired_at_utc']}: "
            f"status {latest['acquisition_status']}; "
            f"weekly used {latest['weekly_used_percent']} percent; "
            f"weekly remaining {latest['weekly_remaining_percent']} percent; "
            f"weekly reset {latest['weekly_reset_at_utc'] or 'unavailable'}; "
            f"usage limit resets available {reset_count_text}; "
            f"source {latest['source_format'] or latest['source_method']}."
        )
    title = "[TEST] Codex usage monitor"
    message = f"Test notification from Oracle VM at {utc_stamp()}. {snapshot_text} No secrets are included."
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "title": title, "message": message}, sort_keys=True))
        return 0
    detail = send_telegram(cfg, title, message)
    with connect_db(cfg) as con:
        con.execute(
            """
            INSERT INTO notification_events
            (snapshot_id,event_key,event_type,title,message,decision,sent,detail,created_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (None, "manual_test", "manual_test", title, message, "sent", 1, detail, utc_stamp()),
        )
        con.commit()
    print(json.dumps({"status": "sent", "detail": detail}, sort_keys=True))
    return 0


def command_init_db(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    init_db(cfg)
    print(json.dumps({"db": str(cfg.db_path), "status": "ok"}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex usage monitor backed only by SQLite")
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("once", help="Collect one snapshot and dispatch eligible notifications")
    once.add_argument("--dry-run-notifications", action="store_true")
    once.set_defaults(func=command_once)
    migrate = sub.add_parser("migrate-legacy-csv", help="Import legacy CSV rows once")
    migrate.add_argument("legacy_csv")
    migrate.add_argument("--backup-path", default="")
    migrate.set_defaults(func=command_migrate)
    sub.add_parser("init-db", help="Create or upgrade the SQLite schema").set_defaults(func=command_init_db)
    sub.add_parser("status", help="Print latest SQLite state as JSON").set_defaults(func=command_status)
    notify = sub.add_parser("notify-test", help="Send one explicit Telegram TEST notification")
    notify.add_argument("--dry-run", action="store_true")
    notify.set_defaults(func=command_notify_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except MonitorError as exc:
        print(json.dumps({"status": "error", "error": sanitize(exc, 700)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
