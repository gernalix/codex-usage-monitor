#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Iterable


VERSION = "2026.08.05"
APP_NAME = "codex-session-archive"
DEFAULT_CODEX_DIR = Path.home() / ".codex"
DEFAULT_SOURCE_ROOT = DEFAULT_CODEX_DIR / "sessions"
DEFAULT_ARCHIVE_ROOT = Path.home() / ".local/share/codex-session-archive"
DEFAULT_SCRIPT_PATH = Path.home() / "projects/codex-usage-monitor/codex_session_archive.py"
DEFAULT_UNIFIED_SCRIPT_PATH = Path.home() / "projects/codex-usage-monitor/codex_usage_monitor.py"
SYSTEMD_USER_DIR = Path.home() / ".config/systemd/user"

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
PROMPT_RE = re.compile(r"\bPROMPT_ID\s*[:=]\s*([A-Za-z0-9_.-]+)\b")
SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|access[_-]?key|refresh[_-]?token|session[_-]?token|id[_-]?token)",
    re.I,
)
SENSITIVE_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgAAAAA[A-Za-z0-9_-]{80,}={0,2}\b"), "[ENCRYPTED_CONTENT_OMITTED_FROM_NORMALIZED_RAW_AVAILABLE]"),
    (re.compile(r"(api\.telegram\.org/bot)[^/\s]+"), r"\1[REDACTED]"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "[TELEGRAM_TOKEN_REDACTED]"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"), "[OPENAI_KEY_REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "[GITHUB_TOKEN_REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"), "[JWT_REDACTED]"),
    (re.compile(r'("(?:accessToken|sessionToken|idToken|refreshToken|token)"\s*:\s*")[^"]+(")', re.I), r"\1[REDACTED]\2"),
)


class ArchiveError(RuntimeError):
    pass


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        result = super().__exit__(exc_type, exc, tb)
        self.close()
        return bool(result)


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in table_columns(con, table):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


class ExclusiveLock:
    def __init__(self, path: Path, *, blocking: bool = False):
        self.path = path
        self.blocking = blocking
        self.handle: Any = None

    def __enter__(self) -> "ExclusiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        try:
            flags = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(self.handle.fileno(), flags)
        except BlockingIOError as exc:
            raise ArchiveError(f"another {APP_NAME} execution already holds {self.path}") from exc
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
    return (value or utc_now()).astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    try:
        return utc_stamp(dt.datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return text


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gzip_payload_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def enforce_private_tree(root: Path) -> None:
    if not root.exists():
        return
    for current, dirs, files in os.walk(root):
        os.chmod(current, 0o700)
        for name in dirs:
            os.chmod(Path(current) / name, 0o700)
        for name in files:
            os.chmod(Path(current) / name, 0o600)


def ensure_archive_layout(root: Path) -> None:
    for rel in (
        "index",
        "locks",
        "tmp",
        "raw/sessions",
        "raw/history",
        "raw/shell_snapshots",
        "normalized/sessions",
        "markdown/sessions",
        "manifests/sessions",
        "metadata",
        "exports",
        "docs",
    ):
        ensure_private_dir(root / rel)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def stable_json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def atomic_write(path: Path, text: str) -> None:
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def atomic_gzip_copy(src: Path, dst: Path) -> None:
    ensure_private_dir(dst.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    try:
        with src.open("rb") as inp, os.fdopen(fd, "wb") as raw_out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz:
                shutil.copyfileobj(inp, gz)
            raw_out.flush()
            os.fsync(raw_out.fileno())
        os.replace(tmp_name, dst)
        os.chmod(dst, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def snapshot_source(src: Path, temp_parent: Path) -> tuple[Path, str, int]:
    ensure_private_dir(temp_parent)
    digest = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{src.name}.snapshot.", suffix=".tmp", dir=temp_parent)
    size = 0
    try:
        with src.open("rb") as inp, os.fdopen(fd, "wb") as out:
            for chunk in iter(lambda: inp.read(1024 * 1024), b""):
                digest.update(chunk)
                out.write(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp_name, 0o600)
        return Path(tmp_name), digest.hexdigest(), size
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def redact_text(value: str) -> str:
    text = value.replace("\x00", " ")
    for pattern, replacement in SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(value: Any, key_hint: str = "") -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key == "encrypted_content":
                redacted[key] = "[ENCRYPTED_CONTENT_OMITTED_FROM_NORMALIZED_RAW_AVAILABLE]"
            elif SENSITIVE_KEY_RE.search(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_obj(item, str(key))
        return redacted
    if isinstance(value, list):
        return [redact_obj(item, key_hint) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def extract_text(payload: dict[str, Any]) -> str:
    if "message" in payload and isinstance(payload["message"], str):
        return payload["message"]
    if "output" in payload and isinstance(payload["output"], str):
        return payload["output"]
    if "output" in payload and isinstance(payload["output"], list):
        parts = []
        for item in payload["output"]:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    if "arguments" in payload and isinstance(payload["arguments"], str):
        return payload["arguments"]
    content = payload.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts = [str(item) for item in summary if str(item)]
        if parts:
            return "\n".join(parts)
    return ""


def first_uuid_from_path(path: Path) -> str | None:
    match = UUID_RE.search(str(path))
    return match.group(0) if match else None


def repo_info_for_cwd(cwd: str | None) -> dict[str, str | None]:
    if not cwd:
        return {"repo_root": None, "git_branch": None, "git_sha": None, "git_remote": None}
    path = Path(cwd)
    if not path.exists():
        return {"repo_root": None, "git_branch": None, "git_sha": None, "git_remote": None}
    try:
        root = subprocess.check_output(["git", "-C", str(path), "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL).strip()
        branch = subprocess.check_output(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        sha = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        remote = subprocess.run(["git", "-C", str(path), "config", "--get", "remote.origin.url"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout.strip() or None
        return {"repo_root": root, "git_branch": branch, "git_sha": sha, "git_remote": remote}
    except subprocess.CalledProcessError:
        return {"repo_root": None, "git_branch": None, "git_sha": None, "git_remote": None}


def connect_db(root: Path) -> sqlite3.Connection:
    ensure_private_dir(root)
    db_path = root / "index/archive.sqlite"
    ensure_private_dir(db_path.parent)
    con = sqlite3.connect(db_path, timeout=30, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def migrate_schema(con: sqlite3.Connection) -> None:
    session_columns = table_columns(con, "sessions")
    if not session_columns:
        return
    add_column_if_missing(con, "sessions", "classification", "classification TEXT NOT NULL DEFAULT 'unknown'")
    add_column_if_missing(con, "sessions", "task_complete_observed", "task_complete_observed INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(con, "sessions", "last_event_type", "last_event_type TEXT")
    add_column_if_missing(con, "sessions", "last_event_subtype", "last_event_subtype TEXT")
    add_column_if_missing(con, "sessions", "partial_tail_lines", "partial_tail_lines INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(con, "sessions", "invalid_internal_json_lines", "invalid_internal_json_lines INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(con, "sessions", "normalized_sha256", "normalized_sha256 TEXT")
    add_column_if_missing(con, "sessions", "markdown_sha256", "markdown_sha256 TEXT")
    add_column_if_missing(con, "sessions", "manifest_sha256", "manifest_sha256 TEXT")
    add_column_if_missing(con, "sessions", "out_of_order_timestamps", "out_of_order_timestamps INTEGER NOT NULL DEFAULT 0")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS prompts (
            prompt_id TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS session_prompts (
            archive_id TEXT NOT NULL REFERENCES sessions(archive_id) ON DELETE CASCADE,
            prompt_id TEXT NOT NULL REFERENCES prompts(prompt_id) ON DELETE CASCADE,
            PRIMARY KEY (archive_id, prompt_id)
        );
        CREATE INDEX IF NOT EXISTS idx_session_prompts_prompt ON session_prompts(prompt_id, archive_id);
        """
    )
    rows = con.execute("SELECT archive_id, prompt_ids FROM sessions WHERE prompt_ids != ''").fetchall()
    for row in rows:
        for prompt_id in [part.strip() for part in str(row["prompt_ids"]).split(",") if part.strip()]:
            con.execute("INSERT OR IGNORE INTO prompts(prompt_id) VALUES (?)", (prompt_id,))
            con.execute(
                "INSERT OR IGNORE INTO session_prompts(archive_id,prompt_id) VALUES (?,?)",
                (row["archive_id"], prompt_id),
            )
    rows = con.execute("SELECT * FROM sessions").fetchall()
    for row in rows:
        updates: dict[str, Any] = {}
        if row["classification"] == "unknown":
            updates["classification"] = row["status"] if row["status"] in {"complete", "incomplete"} else "invalid_internal_json"
        if not row["task_complete_observed"] and row["status"] == "complete":
            updates["task_complete_observed"] = 1
        for column, path_column in (
            ("normalized_sha256", "normalized_path"),
            ("markdown_sha256", "markdown_path"),
            ("manifest_sha256", "manifest_path"),
        ):
            if not row[column]:
                path = Path(row[path_column])
                if path.exists() and not path.is_symlink():
                    updates[column] = file_sha256(path)
        if updates:
            assignments = ", ".join(f"{key}=?" for key in updates)
            con.execute(
                f"UPDATE sessions SET {assignments} WHERE archive_id=?",
                [*updates.values(), row["archive_id"]],
            )


def init_db(root: Path) -> None:
    with connect_db(root) as con:
        legacy = con.execute("PRAGMA table_info(sessions)").fetchall()
        if legacy and not any(row["name"] == "archive_id" for row in legacy):
            con.executescript(
                """
                ALTER TABLE sessions RENAME TO sessions_legacy_session_id_pk;
                DROP INDEX IF EXISTS idx_sessions_first_timestamp;
                DROP INDEX IF EXISTS idx_sessions_cwd;
                DROP INDEX IF EXISTS idx_sessions_repo_root;
                DROP INDEX IF EXISTS idx_sessions_prompt_ids;
                DROP INDEX IF EXISTS idx_sessions_status;
                """
            )
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                archive_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                first_timestamp_utc TEXT,
                last_timestamp_utc TEXT,
                source_path TEXT NOT NULL,
                raw_archive_path TEXT NOT NULL,
                normalized_path TEXT NOT NULL,
                markdown_path TEXT NOT NULL,
                manifest_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL,
                raw_archive_size_bytes INTEGER NOT NULL,
                raw_archive_sha256 TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                invalid_json_lines INTEGER NOT NULL,
                status TEXT NOT NULL,
                cwd TEXT,
                repo_root TEXT,
                git_branch TEXT,
                git_sha TEXT,
                git_remote TEXT,
                prompt_ids TEXT NOT NULL,
                cli_version TEXT,
                imported_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE(source_path)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_first_timestamp ON sessions(first_timestamp_utc);
            CREATE INDEX IF NOT EXISTS idx_sessions_cwd ON sessions(cwd);
            CREATE INDEX IF NOT EXISTS idx_sessions_repo_root ON sessions(repo_root);
            CREATE INDEX IF NOT EXISTS idx_sessions_prompt_ids ON sessions(prompt_ids);
            CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

            CREATE TABLE IF NOT EXISTS source_files (
                source_path TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                archive_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL,
                archive_sha256 TEXT NOT NULL,
                archive_size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                imported_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS import_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                status TEXT NOT NULL,
                sessions_seen INTEGER NOT NULL DEFAULT 0,
                sessions_imported INTEGER NOT NULL DEFAULT 0,
                sessions_skipped INTEGER NOT NULL DEFAULT 0,
                source_files_imported INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            """
        )
        migrate_schema(con)
        con.commit()


def normalize_session(path: Path, source_path: Path | None = None) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    display_path = source_path or path
    session_id = first_uuid_from_path(display_path) or display_path.stem
    events: list[dict[str, Any]] = []
    prompt_ids: set[str] = set()
    first_ts: str | None = None
    last_ts: str | None = None
    cwd: str | None = None
    cli_version: str | None = None
    invalid = 0
    last_invalid_line_no: int | None = None
    partial_tail_lines = 0
    saw_complete = False
    type_counts: dict[str, int] = {}
    last_event_type: str | None = None
    last_event_subtype: str | None = None
    previous_ts: str | None = None
    out_of_order_timestamps = 0
    ends_with_newline = True
    try:
        with path.open("rb") as raw_handle:
            raw_handle.seek(0, os.SEEK_END)
            if raw_handle.tell() > 0:
                raw_handle.seek(-1, os.SEEK_END)
                ends_with_newline = raw_handle.read(1) == b"\n"
    except OSError:
        ends_with_newline = True

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                last_invalid_line_no = line_no
                continue
            top_type = str(obj.get("type") or "")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            payload_type = str(payload.get("type") or "")
            ts = parse_ts(obj.get("timestamp"))
            if ts and previous_ts and ts < previous_ts:
                out_of_order_timestamps += 1
            if ts and first_ts is None:
                first_ts = ts
            if ts:
                last_ts = ts
                previous_ts = ts
            if top_type == "session_meta":
                session_id = str(payload.get("session_id") or payload.get("id") or session_id)
                cwd = str(payload.get("cwd") or cwd or "") or None
                cli_version = str(payload.get("cli_version") or cli_version or "") or None
            elif top_type == "turn_context":
                cwd = str(payload.get("cwd") or cwd or "") or None
            if payload_type == "task_complete":
                saw_complete = True
            last_event_type = top_type or None
            last_event_subtype = payload_type or None
            text = extract_text(payload)
            for match in PROMPT_RE.finditer(text):
                prompt_ids.add(match.group(1))
            subtype = payload_type or str(payload.get("name") or "")
            role = payload.get("role")
            type_counts[top_type] = type_counts.get(top_type, 0) + 1
            event = {
                "schema": "codex-session-archive.event.v1",
                "session_id": session_id,
                "event_index": len(events),
                "source_path": str(display_path),
                "line_no": line_no,
                "timestamp_utc": ts,
                "top_type": top_type,
                "subtype": subtype or None,
                "role": role if isinstance(role, str) else None,
                "tool_name": payload.get("name") if payload.get("type") == "function_call" else None,
                "call_id": payload.get("call_id") if isinstance(payload.get("call_id"), str) else None,
                "content_text": redact_text(text),
                "payload_redacted": redact_obj(payload),
            }
            events.append(event)

    if invalid and last_invalid_line_no is not None and not ends_with_newline:
        partial_tail_lines = 1
    invalid_internal_json_lines = max(0, invalid - partial_tail_lines)
    if invalid_internal_json_lines:
        status = "corrupt"
        classification = "invalid_internal_json"
    elif partial_tail_lines:
        status = "partial"
        classification = "partial_tail"
    elif saw_complete:
        status = "complete"
        classification = "complete"
    else:
        status = "incomplete"
        classification = "incomplete"
    repo = repo_info_for_cwd(cwd)
    manifest = {
        "schema": "codex-session-archive.manifest.v1",
        "session_id": session_id,
        "source_path": str(display_path),
        "first_timestamp_utc": first_ts,
        "last_timestamp_utc": last_ts,
        "event_count": len(events),
        "invalid_json_lines": invalid,
        "invalid_internal_json_lines": invalid_internal_json_lines,
        "partial_tail_lines": partial_tail_lines,
        "status": status,
        "classification": classification,
        "task_complete_observed": saw_complete,
        "last_event_type": last_event_type,
        "last_event_subtype": last_event_subtype,
        "out_of_order_timestamps": out_of_order_timestamps,
        "cwd": cwd,
        "prompt_ids": sorted(prompt_ids),
        "cli_version": cli_version,
        "type_counts": type_counts,
        **repo,
        "repository_state_note": "repo_root, git_branch, git_sha, and git_remote are observed at import time from cwd, not necessarily at the original Codex session time.",
        "limits": {
            "private_chain_of_thought": "not exposed by Codex and not claimable",
            "encrypted_content": "preserved only in raw archive; omitted from normalized payload",
        },
    }
    return session_id, events, manifest


def rel_session_path(path: Path, source_root: Path) -> Path:
    try:
        return path.relative_to(source_root)
    except ValueError:
        return Path(path.name)


def archive_id_for_path(path: Path) -> str:
    return first_uuid_from_path(path) or hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]


def markdown_for_session(manifest: dict[str, Any], events: list[dict[str, Any]]) -> str:
    lines = [
        f"# Codex Session {manifest['session_id']}",
        "",
        f"- status: {manifest['status']}",
        f"- source_path: {manifest['source_path']}",
        f"- first_timestamp_utc: {manifest.get('first_timestamp_utc')}",
        f"- last_timestamp_utc: {manifest.get('last_timestamp_utc')}",
        f"- cwd: {manifest.get('cwd')}",
        f"- repo_root: {manifest.get('repo_root')}",
        f"- prompt_ids: {', '.join(manifest.get('prompt_ids') or [])}",
        f"- event_count: {manifest.get('event_count')}",
        "",
        "## Events",
        "",
    ]
    for event in events:
        head = f"### {event['event_index']} {event.get('timestamp_utc')} {event.get('top_type')}"
        if event.get("subtype"):
            head += f"/{event['subtype']}"
        if event.get("role"):
            head += f" role={event['role']}"
        lines.extend([head, ""])
        text = event.get("content_text") or ""
        if text:
            lines.extend(["```text", text.rstrip(), "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def import_session(root: Path, source_root: Path, path: Path) -> tuple[str, bool, dict[str, Any]]:
    init_db(root)
    if path.is_symlink():
        raise ArchiveError(f"unsafe symlink source session: {path}")
    if not is_relative_to(path, source_root):
        raise ArchiveError(f"source session path is outside source root: {path}")
    archive_id = archive_id_for_path(path)
    snapshot_path, source_sha, source_size = snapshot_source(path, root / "tmp")
    try:
        with connect_db(root) as con:
            row = con.execute("SELECT source_sha256 FROM sessions WHERE source_path=?", (str(path),)).fetchone()
            if row and row["source_sha256"] == source_sha:
                existing = con.execute("SELECT archive_id, session_id, status FROM sessions WHERE source_path=?", (str(path),)).fetchone()
                return str(existing["archive_id"]), False, {"session_id": existing["session_id"], "status": existing["status"]}

        session_id, events, manifest = normalize_session(snapshot_path, source_path=path)
        manifest["archive_id"] = archive_id
        rel = rel_session_path(path, source_root)
        raw_path = root / "raw/sessions" / rel.with_suffix(rel.suffix + ".gz")
        normalized_path = root / "normalized/sessions" / f"{archive_id}.jsonl"
        markdown_path = root / "markdown/sessions" / f"{archive_id}.md"
        manifest_path = root / "manifests/sessions" / f"{archive_id}.json"

        atomic_gzip_copy(snapshot_path, raw_path)
        raw_sha = file_sha256(raw_path)
        raw_size = raw_path.stat().st_size
        normalized_text = "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events)
        normalized_sha = text_sha256(normalized_text)
        atomic_write(normalized_path, normalized_text)
        markdown_text = markdown_for_session(manifest, events)
        markdown_sha = text_sha256(markdown_text)
        atomic_write(markdown_path, markdown_text)
        manifest.update(
            {
                "source_sha256": source_sha,
                "source_size_bytes": source_size,
                "raw_archive_path": str(raw_path),
                "raw_archive_sha256": raw_sha,
                "raw_archive_size_bytes": raw_size,
                "normalized_path": str(normalized_path),
                "normalized_sha256": normalized_sha,
                "markdown_path": str(markdown_path),
                "markdown_sha256": markdown_sha,
                "manifest_path": str(manifest_path),
                "imported_by": f"{APP_NAME} {VERSION}",
                "updated_at_utc": utc_stamp(),
            }
        )
        manifest_text = stable_json_text(manifest)
        manifest_sha = text_sha256(manifest_text)
        atomic_write(manifest_path, manifest_text)
        manifest["manifest_sha256"] = manifest_sha
    finally:
        try:
            snapshot_path.unlink()
        except FileNotFoundError:
            pass

    with connect_db(root) as con:
        now = utc_stamp()
        con.execute(
            """
            INSERT INTO sessions (
                archive_id, session_id, first_timestamp_utc, last_timestamp_utc, source_path,
                raw_archive_path, normalized_path, markdown_path, manifest_path,
                source_sha256, source_size_bytes, raw_archive_size_bytes, raw_archive_sha256,
                event_count, invalid_json_lines, status, cwd, repo_root, git_branch,
                git_sha, git_remote, prompt_ids, cli_version, imported_at_utc, updated_at_utc,
                classification, task_complete_observed, last_event_type, last_event_subtype,
                partial_tail_lines, invalid_internal_json_lines, normalized_sha256,
                markdown_sha256, manifest_sha256, out_of_order_timestamps
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(archive_id) DO UPDATE SET
                first_timestamp_utc=excluded.first_timestamp_utc,
                last_timestamp_utc=excluded.last_timestamp_utc,
                session_id=excluded.session_id,
                source_path=excluded.source_path,
                raw_archive_path=excluded.raw_archive_path,
                normalized_path=excluded.normalized_path,
                markdown_path=excluded.markdown_path,
                manifest_path=excluded.manifest_path,
                source_sha256=excluded.source_sha256,
                source_size_bytes=excluded.source_size_bytes,
                raw_archive_size_bytes=excluded.raw_archive_size_bytes,
                raw_archive_sha256=excluded.raw_archive_sha256,
                event_count=excluded.event_count,
                invalid_json_lines=excluded.invalid_json_lines,
                status=excluded.status,
                cwd=excluded.cwd,
                repo_root=excluded.repo_root,
                git_branch=excluded.git_branch,
                git_sha=excluded.git_sha,
                git_remote=excluded.git_remote,
                prompt_ids=excluded.prompt_ids,
                cli_version=excluded.cli_version,
                classification=excluded.classification,
                task_complete_observed=excluded.task_complete_observed,
                last_event_type=excluded.last_event_type,
                last_event_subtype=excluded.last_event_subtype,
                partial_tail_lines=excluded.partial_tail_lines,
                invalid_internal_json_lines=excluded.invalid_internal_json_lines,
                normalized_sha256=excluded.normalized_sha256,
                markdown_sha256=excluded.markdown_sha256,
                manifest_sha256=excluded.manifest_sha256,
                out_of_order_timestamps=excluded.out_of_order_timestamps,
                updated_at_utc=excluded.updated_at_utc
            """,
            (
                archive_id,
                session_id,
                manifest.get("first_timestamp_utc"),
                manifest.get("last_timestamp_utc"),
                str(path),
                str(raw_path),
                str(normalized_path),
                str(markdown_path),
                str(manifest_path),
                source_sha,
                source_size,
                raw_size,
                raw_sha,
                manifest["event_count"],
                manifest["invalid_json_lines"],
                manifest["status"],
                manifest.get("cwd"),
                manifest.get("repo_root"),
                manifest.get("git_branch"),
                manifest.get("git_sha"),
                manifest.get("git_remote"),
                ",".join(manifest.get("prompt_ids") or []),
                manifest.get("cli_version"),
                now,
                now,
                manifest["classification"],
                1 if manifest["task_complete_observed"] else 0,
                manifest.get("last_event_type"),
                manifest.get("last_event_subtype"),
                manifest["partial_tail_lines"],
                manifest["invalid_internal_json_lines"],
                manifest["normalized_sha256"],
                manifest["markdown_sha256"],
                manifest["manifest_sha256"],
                manifest["out_of_order_timestamps"],
            ),
        )
        con.execute("DELETE FROM session_prompts WHERE archive_id=?", (archive_id,))
        for prompt_id in manifest.get("prompt_ids") or []:
            con.execute("INSERT OR IGNORE INTO prompts(prompt_id) VALUES (?)", (prompt_id,))
            con.execute(
                "INSERT OR IGNORE INTO session_prompts(archive_id,prompt_id) VALUES (?,?)",
                (archive_id, prompt_id),
            )
        con.commit()
    return archive_id, True, manifest


def import_source_file(root: Path, path: Path, kind: str, archive_rel: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if path.is_symlink():
        raise ArchiveError(f"unsafe symlink source file: {path}")
    source_sha = file_sha256(path)
    source_size = path.stat().st_size
    dst = root / archive_rel
    with connect_db(root) as con:
        row = con.execute("SELECT source_sha256 FROM source_files WHERE source_path=?", (str(path),)).fetchone()
        if row and row["source_sha256"] == source_sha:
            return False
    atomic_gzip_copy(path, dst)
    archive_sha = file_sha256(dst)
    archive_size = dst.stat().st_size
    with connect_db(root) as con:
        now = utc_stamp()
        con.execute(
            """
            INSERT INTO source_files
            (source_path,source_kind,archive_path,source_sha256,source_size_bytes,archive_sha256,archive_size_bytes,status,imported_at_utc,updated_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_path) DO UPDATE SET
                source_sha256=excluded.source_sha256,
                source_size_bytes=excluded.source_size_bytes,
                archive_sha256=excluded.archive_sha256,
                archive_size_bytes=excluded.archive_size_bytes,
                status=excluded.status,
                updated_at_utc=excluded.updated_at_utc
            """,
            (str(path), kind, str(dst), source_sha, source_size, archive_sha, archive_size, "complete", now, now),
        )
        con.commit()
    return True


def export_threads_metadata(root: Path, codex_dir: Path) -> bool:
    db = codex_dir / "state_5.sqlite"
    if not db.exists():
        return False
    rows: list[dict[str, Any]] = []
    uri = f"file:{db}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=15) as con:
            con.row_factory = sqlite3.Row
            for row in con.execute("SELECT * FROM threads ORDER BY updated_at DESC"):
                item = dict(row)
                item["schema"] = "codex-session-archive.thread_metadata.v1"
                rows.append(redact_obj(item))
    except sqlite3.Error:
        return False
    out = root / "metadata/codex_threads.jsonl"
    atomic_write(out, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))
    return True


def import_shell_snapshots(root: Path, codex_dir: Path, session_ids: Iterable[str]) -> int:
    snap_dir = codex_dir / "shell_snapshots"
    if not snap_dir.exists():
        return 0
    wanted = set(session_ids)
    imported = 0
    for path in sorted(snap_dir.glob("*.sh")):
        session_id = path.name.split(".", 1)[0]
        if session_id not in wanted:
            continue
        if import_source_file(root, path, "shell_snapshot", Path("raw/shell_snapshots") / session_id / f"{path.name}.gz"):
            imported += 1
    return imported


def command_import(args: argparse.Namespace) -> int:
    root = Path(args.archive_root).expanduser()
    source_root = Path(args.source_root).expanduser()
    codex_dir = Path(args.codex_dir).expanduser()
    init_db(root)
    ensure_archive_layout(root)
    with ExclusiveLock(root / "locks/import.lock", blocking=True):
        run_started = utc_stamp()
        imported = 0
        skipped = 0
        seen = 0
        source_files = 0
        session_ids: list[str] = []
        with connect_db(root) as con:
            cur = con.execute("INSERT INTO import_runs (started_at_utc,status) VALUES (?,?)", (run_started, "running"))
            run_id = int(cur.lastrowid)
            con.commit()
        try:
            for path in sorted(source_root.glob("**/*.jsonl")):
                seen += 1
                _archive_id, did_import, manifest = import_session(root, source_root, path)
                if manifest.get("session_id"):
                    session_ids.append(str(manifest["session_id"]))
                if did_import:
                    imported += 1
                else:
                    skipped += 1
            if import_source_file(root, codex_dir / "history.jsonl", "history", Path("raw/history/history.jsonl.gz")):
                source_files += 1
            if export_threads_metadata(root, codex_dir):
                source_files += 1
            source_files += import_shell_snapshots(root, codex_dir, session_ids)
            status = "ok"
            error = None
        except Exception as exc:
            status = "error"
            error = redact_text(str(exc))
        with connect_db(root) as con:
            con.execute(
                """
                UPDATE import_runs
                SET completed_at_utc=?, status=?, sessions_seen=?, sessions_imported=?,
                    sessions_skipped=?, source_files_imported=?, error=?
                WHERE run_id=?
                """,
                (utc_stamp(), status, seen, imported, skipped, source_files, error, run_id),
            )
            con.commit()
        print(json.dumps({"status": status, "sessions_seen": seen, "sessions_imported": imported, "sessions_skipped": skipped, "source_files_imported": source_files, "archive_root": str(root)}, sort_keys=True))
        return 0 if status == "ok" else 1


def rows_for_filters(root: Path, args: argparse.Namespace) -> list[sqlite3.Row]:
    clauses = []
    params: list[Any] = []
    joins = []
    if getattr(args, "session_id", None):
        clauses.append("session_id=?")
        params.append(args.session_id)
    if getattr(args, "prompt_id", None):
        joins.append("JOIN session_prompts sp ON sp.archive_id = sessions.archive_id")
        clauses.append("sp.prompt_id=?")
        params.append(args.prompt_id)
    if getattr(args, "cwd", None):
        clauses.append("cwd LIKE ?")
        params.append(f"%{args.cwd}%")
    if getattr(args, "repo", None):
        clauses.append("(repo_root LIKE ? OR git_remote LIKE ?)")
        params.extend([f"%{args.repo}%", f"%{args.repo}%"])
    if getattr(args, "date", None):
        clauses.append("substr(first_timestamp_utc,1,10)=?")
        params.append(args.date)
    sql = "SELECT sessions.* FROM sessions"
    if joins:
        sql += " " + " ".join(joins)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY first_timestamp_utc DESC, session_id DESC"
    limit = getattr(args, "limit", None)
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with connect_db(root) as con:
        return con.execute(sql, params).fetchall()


def command_status(args: argparse.Namespace) -> int:
    root = Path(args.archive_root).expanduser()
    init_db(root)
    with connect_db(root) as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS archive_records,
                   COUNT(DISTINCT session_id) AS unique_session_ids,
                   SUM(status='complete') AS complete,
                   SUM(status='incomplete') AS incomplete,
                   SUM(status='partial') AS partial,
                   SUM(status='corrupt') AS corrupt,
                   SUM(classification='partial_tail') AS partial_tail,
                   SUM(classification='invalid_internal_json') AS invalid_internal_json,
                   SUM(source_size_bytes) AS source_bytes,
                   SUM(raw_archive_size_bytes) AS raw_archive_bytes,
                   MAX(updated_at_utc) AS last_update
            FROM sessions
            """
        ).fetchone()
        last = con.execute("SELECT * FROM import_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    payload = dict(row)
    payload["rollouts_imported"] = payload["archive_records"]
    payload["archive_root"] = str(root)
    payload["last_import_run"] = dict(last) if last else None
    if last:
        payload["source_files_changed"] = last["source_files_imported"]
    print(json.dumps(payload, sort_keys=True))
    return 0


def command_search(args: argparse.Namespace) -> int:
    root = Path(args.archive_root).expanduser()
    init_db(root)
    rows = rows_for_filters(root, args)
    for row in rows:
        print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
    return 0 if rows else 1


def command_verify(args: argparse.Namespace) -> int:
    root = Path(args.archive_root).expanduser()
    init_db(root)
    errors: list[str] = []
    deep = bool(getattr(args, "deep", False))
    with ExclusiveLock(root / "locks/import.lock", blocking=True):
        with connect_db(root) as con:
            session_rows = con.execute("SELECT * FROM sessions").fetchall()
            source_rows = con.execute("SELECT * FROM source_files").fetchall()
        for row in session_rows:
            for key in ("raw_archive_path", "normalized_path", "markdown_path", "manifest_path"):
                path = Path(row[key])
                if not path.exists():
                    errors.append(f"missing {key}: {path}")
                elif path.is_symlink() or not is_relative_to(path, root):
                    errors.append(f"unsafe {key}: {path}")
            raw_path = Path(row["raw_archive_path"])
            if raw_path.exists() and file_sha256(raw_path) != row["raw_archive_sha256"]:
                errors.append(f"hash mismatch raw_archive_path: {raw_path}")
            if raw_path.exists():
                try:
                    if gzip_payload_sha256(raw_path) != row["source_sha256"]:
                        errors.append(f"raw payload hash mismatch: {raw_path}")
                except OSError as exc:
                    errors.append(f"raw gzip unreadable {raw_path}: {exc}")
            norm_path = Path(row["normalized_path"])
            if norm_path.exists():
                events = []
                try:
                    with norm_path.open("r", encoding="utf-8") as handle:
                        for line_no, line in enumerate(handle, 1):
                            event = json.loads(line)
                            if event.get("event_index") != len(events):
                                errors.append(f"event_index mismatch {norm_path}:{line_no}")
                            events.append(event)
                except Exception as exc:
                    errors.append(f"normalized parse failed {norm_path}: {exc}")
                if row["normalized_sha256"] and file_sha256(norm_path) != row["normalized_sha256"]:
                    errors.append(f"hash mismatch normalized_path: {norm_path}")
                if len(events) != row["event_count"]:
                    errors.append(f"event_count mismatch {norm_path}: db={row['event_count']} file={len(events)}")
            markdown_path = Path(row["markdown_path"])
            if markdown_path.exists() and row["markdown_sha256"] and file_sha256(markdown_path) != row["markdown_sha256"]:
                errors.append(f"hash mismatch markdown_path: {markdown_path}")
            manifest_path = Path(row["manifest_path"])
            manifest: dict[str, Any] | None = None
            if manifest_path.exists():
                if row["manifest_sha256"] and file_sha256(manifest_path) != row["manifest_sha256"]:
                    errors.append(f"hash mismatch manifest_path: {manifest_path}")
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    errors.append(f"manifest parse failed {manifest_path}: {exc}")
                if manifest:
                    for key in ("archive_id", "session_id", "event_count", "status", "classification"):
                        if key in manifest and str(manifest[key]) != str(row[key]):
                            errors.append(f"manifest mismatch {key}: {manifest_path}")
                    for key in ("source_sha256", "raw_archive_sha256", "normalized_sha256", "markdown_sha256"):
                        if manifest.get(key) and row[key] and manifest[key] != row[key]:
                            errors.append(f"manifest hash mismatch {key}: {manifest_path}")
            if deep and raw_path.exists():
                try:
                    with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                        with gzip.open(raw_path, "rb") as raw_handle:
                            shutil.copyfileobj(raw_handle, tmp)
                    try:
                        _session_id, regenerated_events, regenerated_manifest = normalize_session(
                            tmp_path,
                            source_path=Path(row["source_path"]),
                        )
                    finally:
                        tmp_path.unlink(missing_ok=True)
                    regenerated_norm = "".join(
                        json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in regenerated_events
                    )
                    if norm_path.exists() and norm_path.read_text(encoding="utf-8") != regenerated_norm:
                        errors.append(f"deep normalized semantic mismatch: {norm_path}")
                except Exception as exc:
                    errors.append(f"deep regeneration failed {raw_path}: {exc}")
        for row in source_rows:
            path = Path(row["archive_path"])
            if not path.exists():
                errors.append(f"missing source archive: {path}")
            elif path.is_symlink() or not is_relative_to(path, root):
                errors.append(f"unsafe source archive: {path}")
            elif file_sha256(path) != row["archive_sha256"]:
                errors.append(f"hash mismatch source archive: {path}")
    result = {"status": "ok" if not errors else "error", "deep": deep, "sessions_checked": len(session_rows), "source_files_checked": len(source_rows), "errors": errors[:50]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not errors else 1


def command_export(args: argparse.Namespace) -> int:
    root = Path(args.archive_root).expanduser()
    rows = rows_for_filters(root, args)
    if not rows:
        raise ArchiveError("no sessions matched export filters")
    ensure_private_dir(root / "exports")
    output = Path(args.output).expanduser() if args.output else root / "exports" / f"codex-session-export-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    ensure_private_dir(output.parent)
    members: list[dict[str, Any]] = []
    manifest = {
        "schema": "codex-session-archive.export.v1",
        "created_at_utc": utc_stamp(),
        "archive_root": str(root),
        "archive_ids": [row["archive_id"] for row in rows],
        "session_ids": sorted({row["session_id"] for row in rows}),
        "archive_record_count": len(rows),
        "members": members,
    }
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        for row in rows:
            sid = row["session_id"]
            aid = row["archive_id"]
            for key, folder in (
                ("manifest_path", "manifests"),
                ("normalized_path", "normalized"),
                ("markdown_path", "markdown"),
                ("raw_archive_path", "raw"),
            ):
                path = Path(row[key])
                if not path.exists():
                    continue
                if path.is_symlink() or not is_relative_to(path, root):
                    raise ArchiveError(f"unsafe export member source: {path}")
                arcname = f"sessions/{sid}/{aid}/{folder}/{path.name}"
                members.append(
                    {
                        "archive_id": aid,
                        "session_id": sid,
                        "kind": folder,
                        "path": arcname,
                        "sha256": file_sha256(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
        manifest["global_sha256"] = text_sha256(
            json.dumps(
                {key: value for key, value in manifest.items() if key != "global_sha256"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        export_manifest = staging / "EXPORT_MANIFEST.json"
        export_manifest.write_text(stable_json_text(manifest), encoding="utf-8")
        with tarfile.open(output, "w:gz") as tar:
            tar.add(export_manifest, arcname="EXPORT_MANIFEST.json")
            for member in members:
                source = next(
                    Path(row[key])
                    for row in rows
                    for key, folder in (
                        ("manifest_path", "manifests"),
                        ("normalized_path", "normalized"),
                        ("markdown_path", "markdown"),
                        ("raw_archive_path", "raw"),
                    )
                    if row["archive_id"] == member["archive_id"] and folder == member["kind"]
                )
                tar.add(source, arcname=member["path"])
    os.chmod(output, 0o600)
    print(json.dumps({"status": "ok", "output": str(output), "archive_record_count": len(rows), "member_count": len(members), "global_sha256": manifest["global_sha256"], "session_ids": sorted({row["session_id"] for row in rows})}, sort_keys=True))
    return 0


def command_validate_export(args: argparse.Namespace) -> int:
    export_path = Path(args.export_path).expanduser()
    errors: list[str] = []
    try:
        with tarfile.open(export_path, "r:gz") as tar:
            try:
                manifest_file = tar.extractfile("EXPORT_MANIFEST.json")
            except KeyError:
                manifest_file = None
            if manifest_file is None:
                errors.append("missing EXPORT_MANIFEST.json")
                manifest = {}
            else:
                manifest = json.loads(manifest_file.read().decode("utf-8"))
            members = manifest.get("members") if isinstance(manifest, dict) else []
            if not isinstance(members, list):
                errors.append("manifest members is not a list")
                members = []
            expected_names = {"EXPORT_MANIFEST.json", *[str(member.get("path")) for member in members if isinstance(member, dict)]}
            actual_names = set(tar.getnames())
            missing = expected_names - actual_names
            extra = actual_names - expected_names
            if missing:
                errors.append(f"missing members: {sorted(missing)[:10]}")
            if extra:
                errors.append(f"unexpected members: {sorted(extra)[:10]}")
            for member in members:
                if not isinstance(member, dict):
                    errors.append("invalid member entry")
                    continue
                name = str(member.get("path") or "")
                if name.startswith("/") or ".." in Path(name).parts:
                    errors.append(f"unsafe member path: {name}")
                    continue
                extracted = tar.extractfile(name)
                if extracted is None:
                    continue
                data = extracted.read()
                digest = hashlib.sha256(data).hexdigest()
                if digest != member.get("sha256"):
                    errors.append(f"member hash mismatch: {name}")
                if len(data) != member.get("size_bytes"):
                    errors.append(f"member size mismatch: {name}")
    except Exception as exc:
        errors.append(f"export parse failed: {redact_text(str(exc))}")
    result = {"status": "ok" if not errors else "error", "export_path": str(export_path), "errors": errors[:50]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not errors else 1


def write_docs(root: Path) -> None:
    readme = f"""# Codex Session Archive

schema_version: codex-session-archive.v1
archive_root: {root}

This directory is maintained by `{APP_NAME}`. It passively imports native Codex
session files from `{DEFAULT_SOURCE_ROOT}` without changing the `codex` command.

Captured data:
- exact gzip copies of native rollout JSONL files under `raw/sessions/`;
- normalized JSONL events under `normalized/sessions/`;
- Markdown views under `markdown/sessions/`;
- per-session manifests under `manifests/sessions/`;
- SQLite index at `index/archive.sqlite`;
- raw history and session metadata snapshots when available.

Limits:
- private chain-of-thought is not exposed by Codex and is not archived as readable text;
- opaque encrypted reasoning blobs remain only in raw files and are omitted from normalized payloads;
- normalized files apply best-effort secret redaction; raw files are exact and protected by `0700`/`0600` permissions.
"""
    schema = """# Normalized Event Schema

Each line in `normalized/sessions/<session_id>.jsonl` is one JSON object:

- `schema`: fixed schema id.
- `session_id`: Codex session/thread id.
- `event_index`: zero-based order within the source JSONL.
- `source_path`, `line_no`: original source location.
- `timestamp_utc`: event timestamp when exposed.
- `top_type`: native Codex top-level event type.
- `subtype`: payload event/tool/message type when exposed.
- `role`: user, assistant, developer, etc. when exposed.
- `tool_name`, `call_id`: tool metadata when exposed.
- `content_text`: extracted readable content with best-effort redaction.
- `payload_redacted`: full exposed payload with best-effort redaction.
"""
    atomic_write(root / "docs/README.md", readme)
    atomic_write(root / "docs/SCHEMA.md", schema)


def command_init(args: argparse.Namespace) -> int:
    root = Path(args.archive_root).expanduser()
    init_db(root)
    ensure_archive_layout(root)
    write_docs(root)
    print(json.dumps({"status": "ok", "archive_root": str(root)}, sort_keys=True))
    return 0


def unit_text(script_path: Path, root: Path) -> str:
    return f"""[Unit]
Description=Import native Codex sessions into AI-readable archive

[Service]
Type=oneshot
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 {script_path} sessions --archive-root {root} import
TimeoutStartSec=300
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths={root}
StandardOutput=journal
StandardError=journal
"""


def timer_text() -> str:
    return """[Unit]
Description=Run Codex session archive import frequently

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true
Unit=codex-session-archive.service

[Install]
WantedBy=timers.target
"""


def command_install_systemd(args: argparse.Namespace) -> int:
    root = Path(args.archive_root).expanduser()
    script_path = Path(args.script_path).expanduser()
    command_init(args)
    ensure_private_dir(SYSTEMD_USER_DIR)
    service = SYSTEMD_USER_DIR / "codex-session-archive.service"
    timer = SYSTEMD_USER_DIR / "codex-session-archive.timer"
    atomic_write(service, unit_text(script_path, root))
    atomic_write(timer, timer_text())
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "codex-session-archive.timer"], check=True)
    print(json.dumps({"status": "installed", "service": str(service), "timer": str(timer), "archive_root": str(root)}, sort_keys=True))
    return 0


def command_uninstall_systemd(args: argparse.Namespace) -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", "codex-session-archive.timer"], check=False)
    for name in ("codex-session-archive.service", "codex-session-archive.timer"):
        path = SYSTEMD_USER_DIR / name
        if path.exists():
            path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(json.dumps({"status": "uninstalled"}, sort_keys=True))
    return 0


def add_search_args(parser: argparse.ArgumentParser, *, include_limit: bool) -> None:
    parser.add_argument("--session-id")
    parser.add_argument("--prompt-id")
    parser.add_argument("--cwd")
    parser.add_argument("--repo")
    parser.add_argument("--date")
    if include_limit:
        parser.add_argument("--limit", type=int, default=20)


def argv_from_search_args(command: str, args: argparse.Namespace) -> list[str]:
    argv = [command]
    for key in ("session_id", "prompt_id", "cwd", "repo", "date"):
        value = getattr(args, key, None)
        if value is not None:
            argv.extend([f"--{key.replace('_', '-')}", str(value)])
    limit = getattr(args, "limit", None)
    if limit is not None:
        argv.extend(["--limit", str(limit)])
    return argv


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--archive-root", default=str(DEFAULT_ARCHIVE_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create archive directories, docs and SQLite index")
    init.set_defaults(func=command_init)
    imp = sub.add_parser("import", help="Import all materialized native Codex session sources")
    imp.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    imp.add_argument("--codex-dir", default=str(DEFAULT_CODEX_DIR))
    imp.set_defaults(func=command_import)
    sub.add_parser("status", help="Print aggregate archive status").set_defaults(func=command_status)
    search = sub.add_parser("search", help="Search indexed sessions")
    add_search_args(search, include_limit=True)
    search.set_defaults(func=command_search)
    verify = sub.add_parser("verify", help="Verify hashes and normalized JSONL parsing")
    verify.add_argument("--deep", action="store_true")
    verify.set_defaults(func=command_verify)
    export = sub.add_parser("export", help="Export matching sessions into one ChatGPT-ready tar.gz")
    add_search_args(export, include_limit=True)
    export.add_argument("--output")
    export.set_defaults(func=command_export)
    validate = sub.add_parser("validate-export", help="Validate a session export tarball")
    validate.add_argument("export_path")
    validate.set_defaults(func=command_validate_export)
    install = sub.add_parser("install-user-systemd", help="Install and start the user timer")
    install.add_argument("--script-path", default=str(DEFAULT_UNIFIED_SCRIPT_PATH))
    install.set_defaults(func=command_install_systemd)
    sub.add_parser("uninstall-user-systemd", help="Remove the user timer and service only").set_defaults(func=command_uninstall_systemd)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Passive archival of native Codex sessions")
    return configure_parser(parser)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ArchiveError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "error", "error": redact_text(str(exc))}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
