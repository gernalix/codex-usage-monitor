#!/usr/bin/env python3
"""Publish readable incremental Codex session dumps to the private data repo."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import codex_session_archive as archive
import codex_usage_publisher_legacy as publisher

VERSION = "2026.09.16"
DEFAULT_SOURCE_ROOT = Path.home() / ".codex/sessions"
DEFAULT_STATE_DIR = publisher.DEFAULT_STATE_DIR
DEFAULT_DATA_REPO = publisher.DEFAULT_DATA_REPO
DEFAULT_DATA_REMOTE = publisher.DEFAULT_DATA_REMOTE
MAX_CHUNK_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_SINGLE_RECORD_BYTES = 90 * 1024 * 1024


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def session_id(path: Path) -> str:
    value = archive.first_uuid_from_path(path)
    if value:
        return value
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                payload = obj["payload"]
                return str(payload.get("session_id") or payload.get("id") or path.stem)
    return path.stem


def source_key(path: Path) -> str:
    return digest(str(path.expanduser().resolve()))[:16]


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def write_text(path: Path, text: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return True


def append_chunk(source: Path, offset: int, target: Path) -> tuple[int, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    count = 0
    end = offset
    output_bytes = 0
    try:
        with source.open("rb") as inp, os.fdopen(fd, "w", encoding="utf-8") as out:
            inp.seek(offset)
            while True:
                start = inp.tell()
                raw = inp.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                try:
                    rendered = archive.redact_obj(json.loads(text))
                except json.JSONDecodeError:
                    rendered = {
                        "invalid_json": True,
                        "source_byte_offset": start,
                        "text": archive.redact_text(text),
                    }
                rendered_line = json.dumps(rendered, ensure_ascii=False, sort_keys=True) + "\n"
                encoded_size = len(rendered_line.encode("utf-8"))
                if encoded_size > MAX_SINGLE_RECORD_BYTES:
                    raise publisher.PublisherError("single Codex JSONL record exceeds safe GitHub file size")
                if count and output_bytes + encoded_size > MAX_CHUNK_OUTPUT_BYTES:
                    inp.seek(start)
                    break
                out.write(rendered_line)
                output_bytes += encoded_size
                count += 1
                end = inp.tell()
            out.flush()
            os.fsync(out.fileno())
        if not count:
            os.unlink(tmp)
            return offset, 0
        os.replace(tmp, target)
        return end, count
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def append_rollout(repo: Path, source: Path) -> dict[str, Any]:
    sid = session_id(source)
    key = source_key(source)
    base = repo / "native-sessions" / sid / "sources" / key
    manifest_path = base / "manifest.json"
    manifest = load_json(manifest_path)
    offset = int(manifest.get("complete_through_byte") or 0)
    chunks = int(manifest.get("chunk_count") or 0)
    stat = source.stat()
    size = stat.st_size

    if size < offset:
        key = f"{key}-{digest(f'{stat.st_mtime_ns}:{size}')[:8]}"
        base = repo / "native-sessions" / sid / "sources" / key
        manifest_path = base / "manifest.json"
        manifest = load_json(manifest_path)
        offset = int(manifest.get("complete_through_byte") or 0)
        chunks = int(manifest.get("chunk_count") or 0)

    total_records = 0
    while size > offset:
        target = base / "chunks" / f"{chunks + 1:06d}.jsonl"
        end, records = append_chunk(source, offset, target)
        if not records:
            break
        offset = end
        chunks += 1
        total_records += records

    if not total_records:
        return {"changed": False, "records": 0}

    payload = {
        "schema": "codex-usage.native-session-dump.v1",
        "generated_by": f"codex_chat_dump_publisher {VERSION}",
        "session_id": sid,
        "source_path": str(source),
        "source_key": key,
        "complete_through_byte": offset,
        "source_size_bytes_observed": size,
        "chunk_count": chunks,
        "format": "redacted native JSONL in ordered incremental chunks",
        "updated_at_utc": publisher.utc_stamp(),
    }
    write_text(manifest_path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"changed": True, "records": total_records}


def rebuild_index(repo: Path) -> None:
    rows: list[dict[str, Any]] = []
    for path in sorted((repo / "native-sessions").glob("*/sources/*/manifest.json")):
        manifest = load_json(path)
        if manifest:
            rows.append({
                "session_id": manifest.get("session_id"),
                "source_path": manifest.get("source_path"),
                "source_key": manifest.get("source_key"),
                "complete_through_byte": manifest.get("complete_through_byte"),
                "chunk_count": manifest.get("chunk_count"),
                "path": path.parent.relative_to(repo).as_posix(),
            })
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    write_text(repo / "index/native-sessions.jsonl", text)


def dirty_paths(repo: Path) -> list[str]:
    result = publisher.run(["git", "status", "--porcelain"], repo)
    if result.returncode != 0:
        raise publisher.PublisherError("git status failed")
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def owned(path: str) -> bool:
    return path == "index/native-sessions.jsonl" or path.startswith("native-sessions/")


def ahead(repo: Path) -> int:
    result = publisher.run(["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"], repo)
    if result.returncode != 0:
        return 0
    parts = result.stdout.split()
    try:
        return int(parts[0]) if len(parts) == 2 else 0
    except ValueError:
        return 0


def command_run(args: argparse.Namespace) -> int:
    root = Path(args.source_root).expanduser()
    state_dir = Path(args.state_dir).expanduser()
    repo = Path(args.data_repo).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    publisher.assert_private_repo(args.remote)
    with publisher.ExclusiveLock(state_dir / "publisher.lock"):
        publisher.ensure_repo(repo, args.remote)
        foreign = [path for path in dirty_paths(repo) if not owned(path)]
        if foreign:
            raise publisher.PublisherError(f"codex-usage worktree has unrelated changes: {foreign[:5]}")
        seen = changed = records = 0
        for source in sorted(root.glob("**/*.jsonl")):
            if not source.is_file():
                continue
            seen += 1
            result = append_rollout(repo, source)
            if result["changed"]:
                changed += 1
                records += int(result["records"])
        rebuild_index(repo)
        if any(owned(path) for path in dirty_paths(repo)):
            publisher.git_ok(["git", "add", "--", "native-sessions", "index/native-sessions.jsonl"], repo)
            staged = publisher.run(["git", "diff", "--cached", "--quiet"], repo)
            if staged.returncode == 1:
                publisher.git_ok(["git", "commit", "-m", "usage: sync complete Codex chat dumps"], repo)
            elif staged.returncode != 0:
                raise publisher.PublisherError("git diff --cached failed")
        pushed = False
        if ahead(repo) > 0 and not args.no_push:
            publisher.git_ok(["git", "push", "origin", "main"], repo, timeout=240)
            pushed = True
        print(json.dumps({"status": "published" if pushed else "noop", "sources_seen": seen, "sources_changed": changed, "records_written": records}, sort_keys=True))
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish readable Codex native-session dumps")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--data-repo", default=str(DEFAULT_DATA_REPO))
    parser.add_argument("--remote", default=DEFAULT_DATA_REMOTE)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--no-push", action="store_true")
    run_p.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except publisher.PublisherError as exc:
        print(json.dumps({"status": "error", "error": archive.redact_text(str(exc))}, sort_keys=True))
        return 75
    except BlockingIOError:
        print(json.dumps({"status": "locked"}, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
