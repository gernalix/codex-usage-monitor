#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any

from . import legacy
from .legacy import (
    DEFAULT_DATA_REMOTE,
    DEFAULT_DATA_REPO,
    DEFAULT_QUOTA_DB,
    DEFAULT_SOURCE_ROOT,
    DEFAULT_STATE_DIR,
    PublisherError,
    assert_private_repo,
    atomic_write,
    build_parser,
    command_status,
    dedupe_cycle_keys,
    digest_text,
    ensure_repo,
    git_ok,
    json_obj,
    prompt_dir,
    quota_index,
    run,
    source_sha,
    utc_stamp,
    write_json,
    write_jsonl,
)


VERSION = "2026.09.16"
FINGERPRINT_SCHEMA = 3
SOURCE_SCAN_GENERATION = f"fingerprint-schema-{FINGERPRINT_SCHEMA}"
GUARD_METADATA_KEYS = {"repo_project", "repo_paths", "repo_projects", "repo_write_projects", "repo_path_kinds"}
_GOAL_PREFIX = '<codex_internal_context source="goal">'
ATTACHMENTS_ROOT = Path.home() / ".codex/attachments"
_ATTACHMENT_PATH_RE = re.compile(r"/[^\s`\"']*\.codex/attachments/[^\s`\"']+")
_PROMPT_ID_FALLBACK_RE = re.compile(r"\bPROMPT_ID\s*[:=]\s*[`*_~]*([A-Za-z0-9_.-]+)\b")
_BLOCKED_FOLLOWUP_PREFIX_RE = re.compile(
    r"^(?:ok(?:ay)?|s[iì]|yes|done|fatto|fatta|eseguito|eseguita|procedi|continua|riprendi|vai|autorizzo|ho\s+fatto|l['’]?ho\s+fatto|go\s+ahead)\b",
    re.I,
)
_legacy_connect_state = legacy.connect_state
_legacy_parse_session = legacy.parse_session
_legacy_export_repo = legacy.export_repo
_legacy_send_batch_telegram = legacy.send_batch_telegram
_legacy_command_run = legacy.command_run
_legacy_status_from_final = legacy.status_from_final
_legacy_chat_metrics = legacy.chat_metrics
_should_export = False
_notify_cycle_objects: set[int] = set()
_guard_candidate_cycles: dict[str, dict[str, Any]] = {}


def mark_export_required() -> None:
    global _should_export
    _should_export = True


def export_required() -> bool:
    return _should_export


def _literal_prompt_id(text: str) -> str | None:
    prompt_id = legacy.archive.prompt_id_from_text(text)
    if prompt_id:
        return prompt_id
    normalized = (text or "").replace(r"\_", "_")
    match = _PROMPT_ID_FALLBACK_RE.search(normalized)
    return match.group(1) if match else None


def prompt_id_from_text(text: str) -> str | None:
    prompt_id = _literal_prompt_id(text)
    if prompt_id:
        return prompt_id

    try:
        attachments_root = ATTACHMENTS_ROOT.resolve()
    except OSError:
        return None
    for raw_path in _ATTACHMENT_PATH_RE.findall(text or ""):
        candidate = Path(raw_path.rstrip(".,;:)]}>")).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if attachments_root not in resolved.parents or not resolved.is_file():
            continue
        try:
            prompt_id = _literal_prompt_id(resolved.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if prompt_id:
            return prompt_id
    return None


def status_from_final(text: str) -> str:
    normalized = (text or "").replace(r"\_", "_")
    match = re.search(
        r"\bRESULT\s*[:=]\s*[`*_~]*\s*(?:(?:goal\s+marcato|status)\s+)?(PASS|BLOCKED|FAIL)\b",
        normalized,
        re.I,
    )
    if match:
        return match.group(1).upper()
    return _legacy_status_from_final(text)


def _is_blocked_followup(text: str) -> bool:
    normalized = " ".join((text or "").strip().split())
    return bool(normalized and len(normalized) <= 240 and _BLOCKED_FOLLOWUP_PREFIX_RE.match(normalized))


def chat_metrics(chat_id: int, cycles: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = _legacy_chat_metrics(chat_id, cycles)
    metrics["prompt_ids"] = _stable_unique(
        [str(prompt_id) for prompt_id in metrics.get("prompt_ids") or [] if prompt_id]
    )
    return metrics


def connect_state(state_dir: Path) -> sqlite3.Connection:
    con = _legacy_connect_state(state_dir)
    columns = {str(row["name"]) for row in con.execute("PRAGMA table_info(cycles)")}
    if "cycle_sha256" not in columns:
        con.execute("ALTER TABLE cycles ADD COLUMN cycle_sha256 TEXT")
    if "fingerprint_schema" not in columns:
        con.execute("ALTER TABLE cycles ADD COLUMN fingerprint_schema INTEGER")
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
    upstream_check = _run_git(["rev-parse", "--verify", "--quiet", "@{u}"], repo)
    if upstream_check.returncode == 0:
        upstream_result = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
        if upstream_result.returncode != 0:
            return {"repo_path": str(repo), "status": "unknown", "branch": branch, "upstream": None, "ahead": None, "behind": None, "dirty_count": None, "fetched": 0, "detail": "upstream_resolve_failed"}
        upstream = upstream_result.stdout.strip()
    elif upstream_check.returncode == 1:
        upstream = None
    else:
        return {"repo_path": str(repo), "status": "unknown", "branch": branch, "upstream": None, "ahead": None, "behind": None, "dirty_count": None, "fetched": 0, "detail": "upstream_check_failed"}
    fetched = 0
    if fetch and upstream:
        remote = upstream.split("/", 1)[0]
        fetch_result = _run_git(["fetch", remote, branch or "HEAD"], repo, timeout=60)
        if fetch_result.returncode != 0:
            return {"repo_path": str(repo), "status": "unknown", "branch": branch, "upstream": upstream, "ahead": None, "behind": None, "dirty_count": None, "fetched": 0, "detail": "fetch_failed"}
        fetched = 1
    status_result = _run_git(["status", "--porcelain"], repo)
    if status_result.returncode != 0:
        return {"repo_path": str(repo), "status": "unknown", "branch": branch, "upstream": upstream, "ahead": None, "behind": None, "dirty_count": None, "fetched": fetched, "detail": "status_failed"}
    status_lines = status_result.stdout.splitlines()
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
    write_repos = _repo_roots(list(metrics.get("repo_write_projects") or []))
    repos = write_repos or _repo_roots(list(metrics.get("repo_projects") or []))
    actionable = bool(write_repos)
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
    return [{"cycle_key": cycle_key, **guard, "actionable": actionable, "idempotent": False} for guard in guard_rows]


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


def _stable_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


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
    return _stable_unique(paths)


def _tool_path_kind(payload: dict[str, Any]) -> str:
    name = str(payload.get("name") or "")
    if name == "apply_patch":
        return "write"
    if name in {"exec_command", "shell"}:
        return "workdir_only"
    return "association"


def _write_paths_from_payload(payload: dict[str, Any]) -> list[str]:
    if _tool_path_kind(payload) != "write":
        return []
    return _explicit_paths_from_payload(payload)


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return ""


def _exec_command_is_mutating(command: Any) -> bool:
    text = _command_text(command)
    if not text:
        return False
    patterns = (
        r"(?im)(?:^|[;&|]\s*)git\s+(?:add|commit|rm|mv|reset|checkout|switch|merge|rebase|cherry-pick|revert|stash|clean)\b",
        r"(?im)(?:^|[;&|]\s*)(?:rm|mv|cp|touch|mkdir|rmdir|truncate|install|tee)\b",
        r"(?im)\bsed\b[^\n;]*\s-i(?:\s|$|[^\w])",
        r"(?im)\bperl\b[^\n;]*\s-(?:pi|ip)\b",
        r"(?im)\bdd\b[^\n;]*\bof=",
        r"(?m)(?:^|[^<])(?:>>|>)\s*[^>&]",
        r"\.write_(?:text|bytes)\s*\(",
        r"\bopen\s*\([^\n,]+,\s*['\"][wa+]",
        r"\bshutil\.(?:copy|copy2|copyfile|move|rmtree)\s*\(",
        r"\bos\.(?:remove|unlink|rename|replace|mkdir|makedirs|rmdir)\s*\(",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _exec_write_paths_from_event(event: dict[str, Any]) -> list[str]:
    if event.get("tool_name") not in {"exec_command", "shell"}:
        return []
    args = _json_obj(event.get("content_text"))
    if not _exec_command_is_mutating(args.get("cmd")):
        return []
    paths: list[str] = []
    for key in ("workdir", "cwd"):
        value = args.get(key)
        if isinstance(value, str) and value.startswith("/"):
            paths.append(value)
    return _stable_unique(paths)


def _apply_patch_write_paths_from_event(
    event: dict[str, Any],
    candidate_roots: list[Path],
    fallback_cwd: str | None,
) -> list[str]:
    if event.get("tool_name") != "apply_patch":
        return []
    raw = event.get("content_text")
    args = _json_obj(raw)
    patch = args.get("patch") if isinstance(args.get("patch"), str) else raw if isinstance(raw, str) else ""
    if not patch:
        return []

    bases: list[Path] = []
    for key in ("workdir", "cwd"):
        value = args.get(key)
        if isinstance(value, str) and value.startswith("/"):
            bases.append(Path(value).expanduser())
    bases.extend(candidate_roots)
    if fallback_cwd:
        bases.append(Path(fallback_cwd).expanduser())
    deduped_bases = [Path(value) for value in _stable_unique([str(path) for path in bases])]

    paths: list[str] = []
    for match in re.finditer(r"(?m)^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", patch):
        raw_path = match.group(1).strip()
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            paths.append(str(path))
            continue

        existing = [base / path for base in deduped_bases if (base / path).exists()]
        if existing:
            paths.append(str(existing[0]))
            continue

        parent_matches = [base / path for base in deduped_bases if (base / path).parent.exists()]
        if len(parent_matches) == 1:
            paths.append(str(parent_matches[0]))

    return _stable_unique(paths)


def _fingerprint(cycle: dict[str, Any]) -> str:
    metrics = {key: value for key, value in cycle["metrics"].items() if key not in GUARD_METADATA_KEYS}
    payload = {"metrics": metrics, "events": cycle["events"]}
    return legacy.digest_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _full_fingerprint(cycle: dict[str, Any]) -> str:
    payload = {"metrics": cycle["metrics"], "events": cycle["events"]}
    return legacy.digest_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def parse_session(
    path: Path,
    con: sqlite3.Connection,
    *,
    legacy_parse_session_fn=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    global _should_export
    parser = legacy_parse_session_fn or _legacy_parse_session
    cycles, events = parser(path, con)
    last_prompt_id: str | None = None
    last_status: str | None = None
    migrated = False

    for cycle in cycles:
        metrics = cycle["metrics"]
        raw_repo_paths = _stable_unique([str(value) for value in (metrics.get("repo_paths") or []) if value])
        metrics["repo_paths"] = raw_repo_paths
        candidate_roots = _repo_roots(raw_repo_paths)
        explicit_repos = [str(repo) for repo in candidate_roots]
        inferred_write_paths: list[str] = []
        for event in cycle.get("events") or []:
            if not isinstance(event, dict):
                continue
            inferred_write_paths.extend(_exec_write_paths_from_event(event))
            inferred_write_paths.extend(
                _apply_patch_write_paths_from_event(event, candidate_roots, metrics.get("repo_project"))
            )
        write_paths = _stable_unique(list(metrics.get("repo_write_paths") or []) + inferred_write_paths)
        write_repos = [str(repo) for repo in _repo_roots(write_paths)]
        if explicit_repos:
            metrics["repo_projects"] = explicit_repos
        else:
            fallback = metrics.get("repo_project")
            metrics["repo_projects"] = [str(repo) for repo in _repo_roots([fallback] if isinstance(fallback, str) else [])]
        metrics["repo_write_projects"] = write_repos
        metrics.pop("repo_write_paths", None)
        if len(write_repos) == 1:
            metrics["repo_project"] = write_repos[0]
        elif metrics["repo_projects"]:
            metrics["repo_project"] = metrics["repo_projects"][0]
        metrics["status"] = status_from_final(str(metrics.get("final_response_redacted") or ""))
        prompt_id = metrics.get("prompt_id")
        prompt_text = str(metrics.get("prompt_text_redacted") or "")
        final_text = str(metrics.get("final_response_redacted") or "")
        user_prompt_id = prompt_id_from_text(prompt_text)
        final_prompt_id = prompt_id_from_text(final_text)

        if not prompt_id:
            if user_prompt_id:
                prompt_id = user_prompt_id
            elif final_prompt_id:
                prompt_id = final_prompt_id
            elif last_prompt_id and not prompt_text.strip():
                # A native cycle with no user message cannot introduce a new
                # task. Treat it as an automatic continuation/resume.
                prompt_id = last_prompt_id
            elif prompt_text.lstrip().startswith(_GOAL_PREFIX) and last_prompt_id:
                prompt_id = last_prompt_id
            elif last_prompt_id and last_status == "BLOCKED" and _is_blocked_followup(prompt_text):
                prompt_id = last_prompt_id
            if prompt_id:
                metrics["prompt_id"] = prompt_id
        if prompt_id:
            last_prompt_id = str(prompt_id)
        else:
            last_prompt_id = None
        last_status = str(metrics.get("status") or "UNKNOWN")

        fingerprint = _fingerprint(cycle)
        cycle["source_sha256"] = fingerprint
        cycle["cycle_sha256"] = fingerprint
        cycle["fingerprint_schema"] = FINGERPRINT_SCHEMA
        key = str(metrics["cycle_key"])
        row = con.execute(
            "SELECT source_sha256,cycle_sha256,published_commit,prompt_id,telegram_sent,fingerprint_schema FROM cycles WHERE cycle_key=?",
            (key,),
        ).fetchone()

        # One-time migration: old rows stored the SHA of the entire growing rollout.
        # Seed the stable per-cycle fingerprint without making every old cycle pending once.
        if row and row["published_commit"] and (row["fingerprint_schema"] or 1) < FINGERPRINT_SCHEMA and row["prompt_id"] == metrics.get("prompt_id"):
            con.execute(
                "UPDATE cycles SET source_sha256=?,cycle_sha256=?,fingerprint_schema=?,updated_at_utc=? WHERE cycle_key=?",
                (fingerprint, fingerprint, FINGERPRINT_SCHEMA, legacy.utc_stamp(), key),
            )
            migrated = True
            source_sha = fingerprint
            cycle_sha = fingerprint
        else:
            source_sha = row["source_sha256"] if row else None
            cycle_sha = row["cycle_sha256"] if row else None

        is_new = row is None or row["published_commit"] is None
        content_changed = row is not None and cycle_sha is not None and row["published_commit"] and source_sha != fingerprint
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


def command_run(
    args,
    *,
    parse_session_fn=None,
    export_repo_fn=None,
    send_batch_telegram_fn=None,
) -> int:
    global _should_export, _notify_cycle_objects, _guard_candidate_cycles
    _should_export = False
    _notify_cycle_objects = set()
    _guard_candidate_cycles = {}
    parse_session_fn = parse_session_fn or parse_session
    export_repo_fn = export_repo_fn or export_repo
    send_batch_telegram_fn = send_batch_telegram_fn or send_batch_telegram
    result = int(
        _legacy_command_run(
            args,
            assert_private_repo_fn=assert_private_repo,
            connect_state_fn=connect_state,
            parse_session_fn=parse_session_fn,
            ensure_repo_fn=ensure_repo,
            export_repo_fn=export_repo_fn,
            run_fn=run,
            send_batch_telegram_fn=send_batch_telegram_fn,
            git_ok_fn=git_ok,
            utc_stamp_fn=utc_stamp,
            source_generation=SOURCE_SCAN_GENERATION,
        )
    )
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
        anomalies = [row for row in guard_rows if row.get("actionable") and row.get("status") not in {"clean_synced"} and not row.get("idempotent")]
        if anomalies:
            print(json.dumps({"git_completion_guard": anomalies}, sort_keys=True))
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return command_run(args)
        return int(command_status(args))
    except PublisherError as exc:
        return legacy.publisher_error_result(exc)
    except BlockingIOError:
        print(json.dumps({"status": "locked"}, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
