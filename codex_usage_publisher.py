#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any

import codex_usage_publisher_legacy as legacy
from codex_usage_publisher_legacy import *  # noqa: F401,F403


VERSION = "2026.09.07"
_GOAL_PREFIX = '<codex_internal_context source="goal">'
_legacy_connect_state = legacy.connect_state
_legacy_parse_session = legacy.parse_session
_legacy_export_repo = legacy.export_repo
_legacy_send_batch_telegram = legacy.send_batch_telegram
_legacy_command_run = legacy.command_run
_legacy_status_from_final = legacy.status_from_final
_should_export = False
_notify_cycle_objects: set[int] = set()
_guard_candidate_cycles: dict[str, dict[str, Any]] = {}


def prompt_id_from_text(text: str) -> str | None:
    prompt_id = legacy.archive.prompt_id_from_text(text)
    if prompt_id:
        return prompt_id
    normalized = (text or "").replace(r"\_", "_")
    match = re.search(r"\bPROMPT_ID\s*[:=]\s*[`*_~]*([A-Za-z0-9_.-]+)\b", normalized)
    return match.group(1) if match else None


def status_from_final(text: str) -> str:
    normalized = (text or "").replace(r"\_", "_")
    match = re.search(r"\bRESULT\s*[:=]\s*[`*_~]*\s*(PASS|BLOCKED|FAIL)\b", normalized, re.I)
    if match:
        return match.group(1).upper()
    return _legacy_status_from_final(text)


def connect_state(state_dir: Path) -> sqlite3.Connection:
    con = _legacy_connect_state(state_dir)
    columns = {str(row["name"]) for row in con.execute("PRAGMA table_info(cycles)")}
    if "cycle_sha256" not in columns:
        con.execute("ALTER TABLE cycles ADD COLUMN cycle_sha256 TEXT")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS git_completion_guard (
            cycle_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_id TEXT,
            repo_path TEXT,
            status TEXT NOT NULL,
            branch TEXT,
            upstream TEXT,
            ahead INTEGER,
            behind INTEGER,
            dirty_count INTEGER,
            fetched INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            checked_at_utc TEXT NOT NULL,
            FOREIGN KEY(cycle_key) REFERENCES cycles(cycle_key) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS git_completion_guard_repos (
            cycle_key TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_id TEXT,
            status TEXT NOT NULL,
            branch TEXT,
            upstream TEXT,
            ahead INTEGER,
            behind INTEGER,
            dirty_count INTEGER,
            fetched INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            checked_at_utc TEXT NOT NULL,
            PRIMARY KEY(cycle_key, repo_path),
            FOREIGN KEY(cycle_key) REFERENCES cycles(cycle_key) ON DELETE CASCADE
        );
        """
    )
    con.commit()
    return con


def _run_git(args: list[str], cwd: Path, *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)


def _repo_root(path_text: str | None) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    cwd = path if path.is_dir() else path.parent
    if not cwd.exists():
        return None
    result = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def classify_git_repo(repo: Path, *, fetch: bool = True) -> dict[str, Any]:
    branch_result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    if branch_result.returncode != 0:
        return {"repo_path": str(repo), "status": "unknown", "branch": None, "upstream": None, "ahead": None, "behind": None, "dirty_count": None, "fetched": 0, "detail": "branch_failed"}
    branch = branch_result.stdout.strip()
    upstream_result = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
    upstream = upstream_result.stdout.strip() if upstream_result.returncode == 0 else None
    fetched = 0
    if fetch and upstream:
        remote = upstream.split("/", 1)[0]
        fetch_result = _run_git(["fetch", remote, branch or "HEAD"], repo, timeout=60)
        if fetch_result.returncode != 0:
            return {"repo_path": str(repo), "status": "unknown", "branch": branch, "upstream": upstream, "ahead": None, "behind": None, "dirty_count": None, "fetched": 0, "detail": "fetch_failed"}
        fetched = 1
    status_lines = _run_git(["status", "--porcelain"], repo).stdout.splitlines()
    dirty_count = len([line for line in status_lines if line.strip()])
    ahead = behind = None
    if upstream:
        counts = _run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], repo)
        parts = counts.stdout.split()
        if counts.returncode != 0 or len(parts) != 2:
            return {"repo_path": str(repo), "status": "unknown", "branch": branch, "upstream": upstream, "ahead": None, "behind": None, "dirty_count": dirty_count, "fetched": fetched, "detail": "rev_list_failed"}
        try:
            ahead, behind = int(parts[0]), int(parts[1])
        except ValueError:
            return {"repo_path": str(repo), "status": "unknown", "branch": branch, "upstream": upstream, "ahead": None, "behind": None, "dirty_count": dirty_count, "fetched": fetched, "detail": "rev_list_malformed"}
    if not upstream:
        status = "dirty" if dirty_count else "no_upstream"
    elif dirty_count:
        status = "dirty"
    elif ahead and behind:
        status = "diverged"
    elif ahead:
        status = "ahead"
    elif behind:
        status = "behind"
    else:
        status = "clean_synced"
    return {
        "repo_path": str(repo),
        "status": status,
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirty_count": dirty_count,
        "fetched": fetched,
        "detail": None,
    }


def _repo_roots(path_texts: list[str]) -> list[Path]:
    repos: dict[str, Path] = {}
    for path_text in path_texts:
        repo = _repo_root(path_text)
        if repo is not None:
            repos[str(repo)] = repo
    return [repos[key] for key in sorted(repos)]


def record_git_completion_guard(con: sqlite3.Connection, cycle: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = cycle["metrics"]
    cycle_key = str(metrics["cycle_key"])
    existing_rows = con.execute("SELECT repo_path,status FROM git_completion_guard_repos WHERE cycle_key=?", (cycle_key,)).fetchall()
    if existing_rows:
        return [{"cycle_key": cycle_key, "repo_path": row["repo_path"], "status": row["status"], "idempotent": True} for row in existing_rows]
    repos = _repo_roots(list(metrics.get("repo_projects") or []))
    guard_rows: list[dict[str, Any]] = []
    if not repos:
        guard_rows.append(
            {
                "repo_path": str(metrics.get("repo_project") or "unknown"),
                "status": "unknown",
                "branch": None,
                "upstream": None,
                "ahead": None,
                "behind": None,
                "dirty_count": None,
                "fetched": 0,
                "detail": "repo_unresolved",
            }
        )
    else:
        guard_rows.extend(classify_git_repo(repo) for repo in repos)
    for guard in guard_rows:
        con.execute(
            """
            INSERT INTO git_completion_guard_repos (
                cycle_key, repo_path, session_id, turn_id, status, branch, upstream,
                ahead, behind, dirty_count, fetched, detail, checked_at_utc
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cycle_key,
                guard["repo_path"],
                metrics["native_session_id"],
                metrics.get("turn_id"),
                guard["status"],
                guard["branch"],
                guard["upstream"],
                guard["ahead"],
                guard["behind"],
                guard["dirty_count"],
                guard["fetched"],
                guard["detail"],
                legacy.utc_stamp(),
            ),
        )
    if guard_rows:
        summary = guard_rows[0]
        con.execute(
            """
            INSERT OR IGNORE INTO git_completion_guard (
                cycle_key, session_id, turn_id, repo_path, status, branch, upstream,
                ahead, behind, dirty_count, fetched, detail, checked_at_utc
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cycle_key,
                metrics["native_session_id"],
                metrics.get("turn_id"),
                f"{len(guard_rows)} repos",
                "multi_repo" if len(guard_rows) > 1 else summary["status"],
                summary["branch"],
                summary["upstream"],
                summary["ahead"],
                summary["behind"],
                summary["dirty_count"],
                max(int(row["fetched"]) for row in guard_rows),
                None,
                legacy.utc_stamp(),
            ),
        )
    return [{"cycle_key": cycle_key, **guard, "idempotent": False} for guard in guard_rows]


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _explicit_paths_from_payload(payload: dict[str, Any]) -> list[str]:
    args = _json_obj(payload.get("arguments")) or _json_obj(payload.get("input"))
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


def _fingerprint(cycle: dict[str, Any]) -> str:
    metrics = {key: value for key, value in cycle["metrics"].items() if key not in {"repo_project", "repo_paths", "repo_projects"}}
    payload = {"metrics": metrics, "events": cycle["events"]}
    return legacy.digest_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _full_fingerprint(cycle: dict[str, Any]) -> str:
    payload = {"metrics": cycle["metrics"], "events": cycle["events"]}
    return legacy.digest_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def parse_session(path: Path, con: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    global _should_export
    cycles, events = _legacy_parse_session(path, con)
    last_prompt_id: str | None = None
    migrated = False

    for cycle in cycles:
        metrics = cycle["metrics"]
        explicit_repos = [str(repo) for repo in _repo_roots(list(metrics.get("repo_paths") or []))]
        if explicit_repos:
            metrics["repo_projects"] = explicit_repos
        else:
            fallback = metrics.get("repo_project")
            metrics["repo_projects"] = [str(repo) for repo in _repo_roots([fallback] if isinstance(fallback, str) else [])]
        if metrics["repo_projects"]:
            metrics["repo_project"] = metrics["repo_projects"][0]
        metrics["status"] = status_from_final(str(metrics.get("final_response_redacted") or ""))
        prompt_id = metrics.get("prompt_id")
        prompt_text = str(metrics.get("prompt_text_redacted") or "")
        final_text = str(metrics.get("final_response_redacted") or "")
        final_prompt_id = prompt_id_from_text(final_text)

        if not prompt_id:
            if final_prompt_id:
                prompt_id = final_prompt_id
            elif prompt_text.lstrip().startswith(_GOAL_PREFIX) and last_prompt_id:
                prompt_id = last_prompt_id
            if prompt_id:
                metrics["prompt_id"] = prompt_id
        if prompt_id:
            last_prompt_id = str(prompt_id)

        fingerprint = _fingerprint(cycle)
        full_fingerprint = _full_fingerprint(cycle)
        cycle["source_sha256"] = fingerprint
        cycle["cycle_sha256"] = fingerprint
        key = str(metrics["cycle_key"])
        row = con.execute(
            "SELECT source_sha256,cycle_sha256,published_commit,prompt_id,telegram_sent FROM cycles WHERE cycle_key=?",
            (key,),
        ).fetchone()

        # One-time migration: old rows stored the SHA of the entire growing rollout.
        # Seed the stable per-cycle fingerprint without making every old cycle pending once.
        if row and row["published_commit"] and row["cycle_sha256"] is None and row["prompt_id"] == metrics.get("prompt_id"):
            con.execute(
                "UPDATE cycles SET source_sha256=?,cycle_sha256=?,updated_at_utc=? WHERE cycle_key=?",
                (fingerprint, fingerprint, legacy.utc_stamp(), key),
            )
            migrated = True
            source_sha = fingerprint
            cycle_sha = fingerprint
        else:
            source_sha = row["source_sha256"] if row else None
            cycle_sha = row["cycle_sha256"] if row else None

        is_new = row is None or row["published_commit"] is None
        content_changed = row is not None and cycle_sha is not None and source_sha not in {fingerprint, full_fingerprint}
        prompt_changed = row is not None and row["prompt_id"] != metrics.get("prompt_id")
        if is_new or content_changed or prompt_changed:
            _should_export = True
        if is_new or prompt_changed:
            _guard_candidate_cycles[key] = cycle
        if is_new or (row is not None and row["published_commit"] and not row["telegram_sent"]):
            _notify_cycle_objects.add(id(cycle))

    if migrated:
        con.commit()
    return cycles, events


def export_repo(repo: Path, cycles: list[dict[str, Any]], chat_events_by_id: dict[int, list[dict[str, Any]]], quota_db: Path) -> None:
    # Do not create a Git commit just because an unfinished rollout appended events.
    if not _should_export:
        return
    _legacy_export_repo(repo, cycles, chat_events_by_id, quota_db)


def send_batch_telegram(cycles: list[dict[str, Any]], dry_run: bool) -> bool:
    selected = [cycle for cycle in cycles if id(cycle) in _notify_cycle_objects]
    if not selected:
        return True
    return _legacy_send_batch_telegram(selected, dry_run)


def _install_runtime() -> None:
    legacy.VERSION = VERSION
    legacy.connect_state = connect_state
    legacy.prompt_id_from_text = prompt_id_from_text
    legacy.status_from_final = status_from_final
    legacy.parse_session = parse_session
    legacy.export_repo = export_repo
    legacy.send_batch_telegram = globals()["send_batch_telegram"]
    # Keep the original publisher tests/mock hooks working through this compatibility layer.
    legacy.assert_private_repo = globals()["assert_private_repo"]
    legacy.ensure_repo = globals()["ensure_repo"]
    legacy.git_ok = globals()["git_ok"]
    legacy.run = globals()["run"]
    legacy.command_run = command_run


def command_run(args) -> int:
    global _should_export, _notify_cycle_objects, _guard_candidate_cycles
    _should_export = False
    _notify_cycle_objects = set()
    _guard_candidate_cycles = {}
    _install_runtime()
    result = int(_legacy_command_run(args))
    if result == 0 and _guard_candidate_cycles:
        state_dir = Path(args.state_dir).expanduser()
        with connect_state(state_dir) as con:
            guard_rows = []
            for cycle in _guard_candidate_cycles.values():
                row = con.execute(
                    "SELECT published_commit FROM cycles WHERE cycle_key=?",
                    (cycle["metrics"]["cycle_key"],),
                ).fetchone()
                if row and row["published_commit"]:
                    guard_rows.extend(record_git_completion_guard(con, cycle))
            con.commit()
        anomalies = [row for row in guard_rows if row.get("status") not in {"clean_synced"} and not row.get("idempotent")]
        if anomalies:
            print(json.dumps({"git_completion_guard": anomalies}, sort_keys=True))
    return result


def main(argv: list[str] | None = None) -> int:
    _install_runtime()
    return int(legacy.main(argv))


_install_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
