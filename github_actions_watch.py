#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

import codex_usage_monitor as quota


VERSION = "2026.09.16"
DEFAULT_OWNER = os.getenv("CODEX_GITHUB_OWNER", "gernalix")
DEFAULT_STATE = Path.home() / ".local/state/codex-usage-publisher/github-actions-watch.json"
DEFAULT_DATA_REPO = Path.home() / "projects/codex-usage"
DEFAULT_REMOTE = "https://github.com/gernalix/codex-usage"
FAILURE_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}
MAX_REPOS = 200
RUNS_PER_REPO = 50
RECENT_EVENTS_LIMIT = 100
CLOSED_RETENTION_DAYS = 30


class WatchError(RuntimeError):
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


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_stamp(value: dt.datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def run(cmd: list[str], cwd: Path | None = None, *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def run_json(cmd: list[str], *, timeout: int = 90) -> Any:
    result = run(cmd, timeout=timeout)
    if result.returncode != 0:
        raise WatchError(quota.sanitize(result.stderr or result.stdout or "command failed", 700))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WatchError("command returned invalid JSON") from exc


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


def load_state(path: Path, owner: str) -> dict[str, Any]:
    if not path.exists():
        return {"schema": 1, "owner": owner, "keys": {}, "recent_events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": 1, "owner": owner, "keys": {}, "recent_events": []}
    if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
        return {"schema": 1, "owner": owner, "keys": {}, "recent_events": []}
    data.setdefault("recent_events", [])
    data["owner"] = owner
    data["schema"] = 1
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def list_repositories(owner: str) -> list[str]:
    payload = run_json(
        ["gh", "repo", "list", owner, "--limit", str(MAX_REPOS), "--source", "--json", "nameWithOwner,isArchived"],
        timeout=90,
    )
    if not isinstance(payload, list):
        raise WatchError("gh repo list returned an unexpected payload")
    repos = []
    for item in payload:
        if not isinstance(item, dict) or item.get("isArchived") is True:
            continue
        name = item.get("nameWithOwner")
        if isinstance(name, str) and name:
            repos.append(name)
    return sorted(set(repos))


def latest_completed_runs(repo: str) -> tuple[list[dict[str, Any]], str | None]:
    result = run(
        ["gh", "api", f"/repos/{repo}/actions/runs?status=completed&per_page={RUNS_PER_REPO}"],
        timeout=90,
    )
    if result.returncode != 0:
        return [], quota.sanitize(result.stderr or result.stdout or "GitHub API error", 500)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], "GitHub API returned invalid JSON"
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    return ([row for row in runs if isinstance(row, dict)] if isinstance(runs, list) else []), None


def incident_key(repo: str, run_row: dict[str, Any]) -> str:
    workflow_id = str(run_row.get("workflow_id") or "unknown")
    branch = str(run_row.get("head_branch") or "")
    return f"{repo}|{workflow_id}|{branch}"


def current_by_key(repo: str, runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in runs:
        key = incident_key(repo, row)
        candidate = {
            "repo": repo,
            "workflow_id": row.get("workflow_id"),
            "workflow_name": row.get("name") or row.get("display_title") or "workflow",
            "branch": row.get("head_branch"),
            "run_id": row.get("id"),
            "run_number": row.get("run_number"),
            "conclusion": row.get("conclusion"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "head_sha": row.get("head_sha"),
            "url": row.get("html_url"),
        }
        previous = latest.get(key)
        if previous is None:
            latest[key] = candidate
            continue
        prev_ts = str(previous.get("created_at") or "")
        cand_ts = str(candidate.get("created_at") or "")
        if (cand_ts, int(candidate.get("run_id") or 0)) > (prev_ts, int(previous.get("run_id") or 0)):
            latest[key] = candidate
    return latest


def add_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    history = [row for row in state.get("recent_events", []) if isinstance(row, dict)]
    history.append(event)
    state["recent_events"] = history[-RECENT_EVENTS_LIMIT:]


def process_scan(
    state: dict[str, Any],
    current: dict[str, dict[str, Any]],
    *,
    observed_at: str,
) -> tuple[list[str], list[str], bool]:
    keys = state.setdefault("keys", {})
    bootstrap = not bool(keys)
    notify_keys: list[str] = []
    event_keys: list[str] = []

    for key, row in current.items():
        conclusion = str(row.get("conclusion") or "")
        failing = conclusion in FAILURE_CONCLUSIONS
        previous = keys.get(key) if isinstance(keys.get(key), dict) else None
        run_id = row.get("run_id")

        if previous is None:
            entry = {
                **row,
                "open": failing,
                "first_failure_at": row.get("created_at") if failing else None,
                "latest_failure_at": row.get("created_at") if failing else None,
                "failure_count": 1 if failing else 0,
                "notification_pending": "failure" if failing else None,
                "last_observed_at": observed_at,
            }
            keys[key] = entry
            if failing:
                event = {
                    "type": "failure",
                    "incident_key": key,
                    "observed_at": observed_at,
                    "repo": row["repo"],
                    "workflow_name": row["workflow_name"],
                    "branch": row.get("branch"),
                    "run_id": run_id,
                    "head_sha": row.get("head_sha"),
                    "url": row.get("url"),
                    "bootstrap": bootstrap,
                }
                add_event(state, event)
                event_keys.append(key)
                notify_keys.append(key)
            continue

        old_open = bool(previous.get("open"))
        old_run_id = previous.get("run_id")
        entry = {**previous, **row, "last_observed_at": observed_at}

        if failing:
            if not old_open:
                entry["open"] = True
                entry["first_failure_at"] = row.get("created_at")
                entry["failure_count"] = 1
                entry["notification_pending"] = "failure"
                event = {
                    "type": "failure",
                    "incident_key": key,
                    "observed_at": observed_at,
                    "repo": row["repo"],
                    "workflow_name": row["workflow_name"],
                    "branch": row.get("branch"),
                    "run_id": run_id,
                    "head_sha": row.get("head_sha"),
                    "url": row.get("url"),
                    "bootstrap": False,
                }
                add_event(state, event)
                event_keys.append(key)
            elif run_id != old_run_id:
                entry["failure_count"] = int(previous.get("failure_count") or 0) + 1
            entry["latest_failure_at"] = row.get("created_at")
        elif conclusion == "success" and old_open:
            entry["open"] = False
            entry["resolved_at"] = row.get("created_at") or observed_at
            entry["notification_pending"] = "recovery"
            event = {
                "type": "recovery",
                "incident_key": key,
                "observed_at": observed_at,
                "repo": row["repo"],
                "workflow_name": row["workflow_name"],
                "branch": row.get("branch"),
                "run_id": run_id,
                "head_sha": row.get("head_sha"),
                "url": row.get("url"),
                "bootstrap": False,
            }
            add_event(state, event)
            event_keys.append(key)
        elif not old_open:
            entry["open"] = False
            entry["notification_pending"] = None

        keys[key] = entry
        if entry.get("notification_pending"):
            notify_keys.append(key)

    # Retry pending notifications even if an incident key was not returned in this scan.
    for key, entry in keys.items():
        if isinstance(entry, dict) and entry.get("notification_pending") and key not in notify_keys:
            notify_keys.append(key)

    return notify_keys, event_keys, bootstrap


def prune_closed(state: dict[str, Any], now: dt.datetime) -> None:
    keys = state.get("keys") if isinstance(state.get("keys"), dict) else {}
    cutoff = now - dt.timedelta(days=CLOSED_RETENTION_DAYS)
    stale = []
    for key, entry in keys.items():
        if not isinstance(entry, dict) or entry.get("open") or entry.get("notification_pending"):
            continue
        seen = parse_ts(entry.get("resolved_at")) or parse_ts(entry.get("updated_at")) or parse_ts(entry.get("created_at"))
        if seen and seen < cutoff:
            stale.append(key)
    for key in stale:
        keys.pop(key, None)


def notification_message(state: dict[str, Any], notify_keys: list[str]) -> str:
    failures: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    keys = state.get("keys") if isinstance(state.get("keys"), dict) else {}
    for key in notify_keys:
        entry = keys.get(key)
        if not isinstance(entry, dict):
            continue
        kind = entry.get("notification_pending")
        if kind == "failure":
            failures.append(entry)
        elif kind == "recovery":
            recoveries.append(entry)

    lines: list[str] = []
    if failures:
        lines.append(f"{len(failures)} GitHub CI incident{'s' if len(failures) != 1 else ''} open")
        for entry in failures[:10]:
            sha = str(entry.get("head_sha") or "")[:7]
            branch = entry.get("branch") or "?"
            lines.append(f"{entry.get('repo')} · {entry.get('workflow_name')} · {branch} · {sha or '?'}")
            if entry.get("url"):
                lines.append(str(entry["url"]))
        if len(failures) > 10:
            lines.append(f"+{len(failures) - 10} altri incidenti")
    if recoveries:
        lines.append(f"{len(recoveries)} GitHub CI incident{'s' if len(recoveries) != 1 else ''} recovered")
        for entry in recoveries[:10]:
            branch = entry.get("branch") or "?"
            lines.append(f"{entry.get('repo')} · {entry.get('workflow_name')} · {branch}")
        if len(recoveries) > 10:
            lines.append(f"+{len(recoveries) - 10} altri recovery")
    return "\n".join(lines)


def dispatch_notification(state: dict[str, Any], notify_keys: list[str], *, dry_run: bool) -> tuple[bool, str]:
    if not notify_keys:
        return True, "none"
    message = notification_message(state, notify_keys)
    if not message:
        return True, "none"
    if dry_run:
        return True, message
    cfg = quota.build_config(None)
    if not cfg.telegram_enabled:
        return False, "telegram disabled"
    try:
        detail = quota.send_telegram(cfg, "GitHub Actions", message)
    except Exception as exc:
        return False, quota.sanitize(exc, 700)
    keys = state.get("keys") if isinstance(state.get("keys"), dict) else {}
    for key in notify_keys:
        entry = keys.get(key)
        if isinstance(entry, dict):
            entry["notification_pending"] = None
            entry["last_notified_at"] = utc_stamp()
    return True, detail


def active_incidents(state: dict[str, Any]) -> list[dict[str, Any]]:
    keys = state.get("keys") if isinstance(state.get("keys"), dict) else {}
    result = []
    for key, entry in keys.items():
        if not isinstance(entry, dict) or not entry.get("open"):
            continue
        result.append({"incident_key": key, **entry})
    return sorted(result, key=lambda row: (str(row.get("repo")), str(row.get("workflow_name")), str(row.get("branch"))))


def public_snapshot(
    state: dict[str, Any],
    *,
    generated_at: str,
    repos_scanned: int,
    scan_errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema": "codex-usage.github-actions.v1",
        "generated_at_utc": generated_at,
        "owner": state.get("owner"),
        "status": "degraded" if scan_errors else "ok",
        "repos_scanned": repos_scanned,
        "active_incident_count": len(active_incidents(state)),
        "active_incidents": active_incidents(state),
        "recent_events": [row for row in state.get("recent_events", []) if isinstance(row, dict)][-RECENT_EVENTS_LIMIT:],
        "scan_errors": scan_errors[:50],
    }


def git_ok(args: list[str], cwd: Path, *, timeout: int = 180) -> str:
    result = run(["git", *args], cwd=cwd, timeout=timeout)
    if result.returncode != 0:
        raise WatchError(quota.sanitize(result.stderr or result.stdout or f"git {' '.join(args)} failed", 700))
    return result.stdout.strip()


def publish_snapshot(repo: Path, remote: str, snapshot: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"status": "dry_run"}
    if not (repo / ".git").exists():
        return {"status": "skipped", "detail": "data repo missing"}
    remote_url = run(["git", "config", "--get", "remote.origin.url"], cwd=repo).stdout.strip()
    if remote_url != remote and remote_url.rstrip(".git") != remote.rstrip(".git"):
        return {"status": "blocked", "detail": "unexpected data repo remote"}
    dirty = run(["git", "status", "--porcelain"], cwd=repo)
    if dirty.returncode != 0:
        return {"status": "blocked", "detail": "git status failed"}
    if dirty.stdout.strip():
        return {"status": "blocked_dirty", "detail": quota.sanitize(dirty.stdout, 500)}

    try:
        git_ok(["fetch", "origin", "main"], repo)
        git_ok(["checkout", "main"], repo)
        git_ok(["pull", "--ff-only", "origin", "main"], repo)
        target = repo / "index/github-actions.json"
        text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        if existing == text:
            return {"status": "noop"}
        atomic_write(target, text)
        git_ok(["add", "index/github-actions.json"], repo)
        staged = run(["git", "diff", "--cached", "--quiet", "--", "index/github-actions.json"], cwd=repo)
        if staged.returncode == 0:
            return {"status": "noop"}
        if staged.returncode != 1:
            raise WatchError("git diff --cached failed")
        git_ok(["commit", "-m", "monitor: update GitHub Actions incidents"], repo)
        git_ok(["push", "origin", "main"], repo, timeout=240)
        commit = git_ok(["rev-parse", "HEAD"], repo)
        return {"status": "published", "commit": commit}
    except WatchError as exc:
        return {"status": "error", "detail": str(exc)}


def command_run(args: argparse.Namespace) -> int:
    state_path = Path(args.state).expanduser()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    now = utc_now()
    observed_at = utc_stamp(now)
    with ExclusiveLock(lock_path):
        state = copy.deepcopy(load_state(state_path, args.owner))
        scan_errors: list[dict[str, str]] = []
        try:
            repos = list_repositories(args.owner)
        except Exception as exc:
            detail = quota.sanitize(exc, 700)
            print(json.dumps({"status": "degraded", "error": detail}, sort_keys=True))
            return 0

        current: dict[str, dict[str, Any]] = {}
        for repo in repos:
            runs, error = latest_completed_runs(repo)
            if error:
                scan_errors.append({"repo": repo, "error": error})
                continue
            current.update(current_by_key(repo, runs))

        notify_keys, event_keys, bootstrap = process_scan(state, current, observed_at=observed_at)
        prune_closed(state, now)
        notification_ok, notification_detail = dispatch_notification(state, notify_keys, dry_run=args.dry_run)
        snapshot = public_snapshot(
            state,
            generated_at=observed_at,
            repos_scanned=len(repos),
            scan_errors=scan_errors,
        )
        publish = publish_snapshot(Path(args.data_repo).expanduser(), args.remote, snapshot, dry_run=args.dry_run)
        if not args.dry_run:
            save_state(state_path, state)
        print(
            json.dumps(
                {
                    "status": "degraded" if scan_errors else "ok",
                    "repos": len(repos),
                    "active_incidents": snapshot["active_incident_count"],
                    "events": len(event_keys),
                    "bootstrap": bootstrap,
                    "notification": "ok" if notification_ok else "failed",
                    "notification_detail": notification_detail,
                    "publish": publish,
                },
                sort_keys=True,
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deduplicated GitHub Actions incident watcher")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--data-repo", default=str(DEFAULT_DATA_REPO))
    parser.add_argument("--remote", default=DEFAULT_REMOTE)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BlockingIOError:
        print(json.dumps({"status": "locked"}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": quota.sanitize(exc, 700)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
