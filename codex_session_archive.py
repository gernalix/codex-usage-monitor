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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


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
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


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
    saw_complete = False
    type_counts: dict[str, int] = {}

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            top_type = str(obj.get("type") or "")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            payload_type = str(payload.get("type") or "")
            ts = parse_ts(obj.get("timestamp"))
            if ts and first_ts is None:
                first_ts = ts
            if ts:
                last_ts = ts
            if top_type == "session_meta":
                session_id = str(payload.get("session_id") or payload.get("id") or session_id)
                cwd = str(payload.get("cwd") or cwd or "") or None
                cli_version = str(payload.get("cli_version") or cli_version or "") or None
            elif top_type == "turn_context":
                cwd = str(payload.get("cwd") or cwd or "") or None
            if payload_type == "task_complete":
                saw_complete = True
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

    status = "corrupt" if invalid else ("complete" if saw_complete else "incomplete")
    repo = repo_info_for_cwd(cwd)
    manifest = {
        "schema": "codex-session-archive.manifest.v1",
        "session_id": session_id,
        "source_path": str(display_path),
        "first_timestamp_utc": first_ts,
        "last_timestamp_utc": last_ts,
        "event_count": len(events),
        "invalid_json_lines": invalid,
        "status": status,
        "cwd": cwd,
        "prompt_ids": sorted(prompt_ids),
        "cli_version": cli_version,
        "type_counts": type_counts,
        **repo,
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
        atomic_write(normalized_path, normalized_text)
        atomic_write(markdown_path, markdown_for_session(manifest, events))
        manifest.update(
            {
                "source_sha256": source_sha,
                "source_size_bytes": source_size,
                "raw_archive_path": str(raw_path),
                "raw_archive_sha256": raw_sha,
                "raw_archive_size_bytes": raw_size,
                "normalized_path": str(normalized_path),
                "markdown_path": str(markdown_path),
                "manifest_path": str(manifest_path),
                "imported_by": f"{APP_NAME} {VERSION}",
                "updated_at_utc": utc_stamp(),
            }
        )
        write_json(manifest_path, manifest)
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
                git_sha, git_remote, prompt_ids, cli_version, imported_at_utc, updated_at_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            ),
        )
        con.commit()
    return archive_id, True, manifest


def import_source_file(root: Path, path: Path, kind: str, archive_rel: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
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
    ensure_private_dir(root)
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
        enforce_private_tree(root)
        print(json.dumps({"status": status, "sessions_seen": seen, "sessions_imported": imported, "sessions_skipped": skipped, "source_files_imported": source_files, "archive_root": str(root)}, sort_keys=True))
        return 0 if status == "ok" else 1


def rows_for_filters(root: Path, args: argparse.Namespace) -> list[sqlite3.Row]:
    clauses = []
    params: list[Any] = []
    if getattr(args, "session_id", None):
        clauses.append("session_id=?")
        params.append(args.session_id)
    if getattr(args, "prompt_id", None):
        clauses.append("(',' || prompt_ids || ',') LIKE ?")
        params.append(f"%,{args.prompt_id},%")
    if getattr(args, "cwd", None):
        clauses.append("cwd LIKE ?")
        params.append(f"%{args.cwd}%")
    if getattr(args, "repo", None):
        clauses.append("(repo_root LIKE ? OR git_remote LIKE ?)")
        params.extend([f"%{args.repo}%", f"%{args.repo}%"])
    if getattr(args, "date", None):
        clauses.append("substr(first_timestamp_utc,1,10)=?")
        params.append(args.date)
    sql = "SELECT * FROM sessions"
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
            SELECT COUNT(*) AS sessions,
                   COUNT(DISTINCT session_id) AS unique_session_ids,
                   SUM(status='complete') AS complete,
                   SUM(status='incomplete') AS incomplete,
                   SUM(status='corrupt') AS corrupt,
                   SUM(source_size_bytes) AS source_bytes,
                   SUM(raw_archive_size_bytes) AS raw_archive_bytes,
                   MAX(updated_at_utc) AS last_update
            FROM sessions
            """
        ).fetchone()
        last = con.execute("SELECT * FROM import_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    payload = dict(row)
    payload["archive_root"] = str(root)
    payload["last_import_run"] = dict(last) if last else None
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
    with ExclusiveLock(root / "locks/import.lock", blocking=True):
        with connect_db(root) as con:
            session_rows = con.execute("SELECT * FROM sessions").fetchall()
            source_rows = con.execute("SELECT * FROM source_files").fetchall()
        for row in session_rows:
            for key in ("raw_archive_path", "normalized_path", "markdown_path", "manifest_path"):
                path = Path(row[key])
                if not path.exists():
                    errors.append(f"missing {key}: {path}")
            raw_path = Path(row["raw_archive_path"])
            if raw_path.exists() and file_sha256(raw_path) != row["raw_archive_sha256"]:
                errors.append(f"hash mismatch raw_archive_path: {raw_path}")
            if raw_path.exists():
                digest = hashlib.sha256()
                try:
                    with gzip.open(raw_path, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != row["source_sha256"]:
                        errors.append(f"raw payload hash mismatch: {raw_path}")
                except OSError as exc:
                    errors.append(f"raw gzip unreadable {raw_path}: {exc}")
            norm_path = Path(row["normalized_path"])
            if norm_path.exists():
                try:
                    with norm_path.open("r", encoding="utf-8") as handle:
                        for line_no, line in enumerate(handle, 1):
                            json.loads(line)
                except Exception as exc:
                    errors.append(f"normalized parse failed {norm_path}:{line_no}: {exc}")
        for row in source_rows:
            path = Path(row["archive_path"])
            if not path.exists():
                errors.append(f"missing source archive: {path}")
            elif file_sha256(path) != row["archive_sha256"]:
                errors.append(f"hash mismatch source archive: {path}")
    result = {"status": "ok" if not errors else "error", "sessions_checked": len(session_rows), "source_files_checked": len(source_rows), "errors": errors[:50]}
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
    manifest = {
        "schema": "codex-session-archive.export.v1",
        "created_at_utc": utc_stamp(),
        "archive_root": str(root),
        "archive_ids": [row["archive_id"] for row in rows],
        "session_ids": sorted({row["session_id"] for row in rows}),
        "archive_record_count": len(rows),
    }
    with tarfile.open(output, "w:gz") as tar:
        with tempfile.TemporaryDirectory() as tmp:
            export_manifest = Path(tmp) / "EXPORT_MANIFEST.json"
            export_manifest.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            tar.add(export_manifest, arcname="EXPORT_MANIFEST.json")
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
                if path.exists():
                    tar.add(path, arcname=f"sessions/{sid}/{aid}/{folder}/{path.name}")
    os.chmod(output, 0o600)
    print(json.dumps({"status": "ok", "output": str(output), "archive_record_count": len(rows), "session_ids": sorted({row["session_id"] for row in rows})}, sort_keys=True))
    return 0


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
    write_docs(root)
    enforce_private_tree(root)
    print(json.dumps({"status": "ok", "archive_root": str(root)}, sort_keys=True))
    return 0


def unit_text(script_path: Path, root: Path) -> str:
    return f"""[Unit]
Description=Import native Codex sessions into AI-readable archive

[Service]
Type=oneshot
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 {script_path} --archive-root {root} import
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
OnUnitActiveSec=1min
AccuracySec=15s
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Passive archival of native Codex sessions")
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
    for target in (search,):
        target.add_argument("--session-id")
        target.add_argument("--prompt-id")
        target.add_argument("--cwd")
        target.add_argument("--repo")
        target.add_argument("--date")
        target.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=command_search)
    verify = sub.add_parser("verify", help="Verify hashes and normalized JSONL parsing")
    verify.set_defaults(func=command_verify)
    export = sub.add_parser("export", help="Export matching sessions into one ChatGPT-ready tar.gz")
    export.add_argument("--session-id")
    export.add_argument("--prompt-id")
    export.add_argument("--cwd")
    export.add_argument("--repo")
    export.add_argument("--date")
    export.add_argument("--limit", type=int)
    export.add_argument("--output")
    export.set_defaults(func=command_export)
    install = sub.add_parser("install-user-systemd", help="Install and start the user timer")
    install.add_argument("--script-path", default=str(DEFAULT_SCRIPT_PATH))
    install.set_defaults(func=command_install_systemd)
    sub.add_parser("uninstall-user-systemd", help="Remove the user timer and service only").set_defaults(func=command_uninstall_systemd)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ArchiveError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "error", "error": redact_text(str(exc))}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
