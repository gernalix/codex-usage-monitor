#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


DEFAULT_OWNER = "gernalix"
DEFAULT_PROJECTS_DIR = Path.home() / "projects"
DEFAULT_STATE_DIR = Path.home() / ".local/state/codex-github-autosync"
DEFAULT_MEGAVAULT = Path.home() / "MegaVault"
GHORG = Path.home() / ".local/bin/ghorg"


class AutosyncError(RuntimeError):
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


def run(
    cmd: list[str],
    cwd: Path | None = None,
    *,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)


def gh_token() -> str:
    result = run(["gh", "auth", "token"], timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        raise AutosyncError("gh_auth_token_unavailable")
    return result.stdout.strip()


def github_repos(owner: str) -> list[dict[str, str]]:
    result = run(
        [
            "gh",
            "repo",
            "list",
            owner,
            "--limit",
            "1000",
            "--json",
            "name,url,defaultBranchRef",
        ],
        timeout=120,
    )
    if result.returncode != 0:
        raise AutosyncError("github_repo_list_failed")
    repos = []
    for item in json.loads(result.stdout):
        branch = item.get("defaultBranchRef") or {}
        repos.append(
            {
                "name": str(item["name"]),
                "url": str(item["url"]),
                "default_branch": str(branch.get("name") or "UNKNOWN"),
            }
        )
    return sorted(repos, key=lambda row: row["name"])


def ghorg_args(owner: str, projects_dir: Path, *, dry_run: bool = False) -> list[str]:
    args = [
        str(GHORG),
        "clone",
        owner,
        "--scm=github",
        "--clone-type=user",
        "--github-user-option=owner",
        "--path",
        str(projects_dir),
        "--output-dir",
        ".",
        "--protocol=https",
        "--protect-local",
        "--fetch-all",
        "--fetch-prune",
        "--no-clean",
        "--no-dir-size",
    ]
    if dry_run:
        args.append("--dry-run")
    return args


def run_ghorg(owner: str, projects_dir: Path, *, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    if not GHORG.exists():
        raise AutosyncError(f"ghorg_missing:{GHORG}")
    env = {**os.environ, "GHORG_GITHUB_TOKEN": gh_token()}
    return run(ghorg_args(owner, projects_dir, dry_run=dry_run), timeout=1800, env=env)


def summarize_ghorg_output(output: str) -> dict[str, int]:
    text = output.lower()
    return {
        "cloned": len(re.findall(r"\bclon(?:e|ed|ing)\b", text)),
        "updated": len(re.findall(r"\b(fetch|pull|update|updated)\b", text)),
        "protected_skipped": len(re.findall(r"(protect-local|uncommitted|unpushed|skip|skipped)", text)),
        "errors": len(re.findall(r"\b(error|failed|fatal)\b", text)),
    }


def register_in_megavault(megavault: Path, projects_dir: Path, repos: list[dict[str, str]], *, dry_run: bool) -> dict[str, int | str]:
    if dry_run:
        return {"already_registered": 0, "newly_registered": 0, "deferred": 0, "validation": "dry_run"}
    if run(["git", "status", "--porcelain"], megavault).stdout.strip():
        return {"already_registered": 0, "newly_registered": 0, "deferred": len(repos), "validation": "deferred_dirty"}
    if run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], megavault).stdout.strip() != "0\t0":
        return {"already_registered": 0, "newly_registered": 0, "deferred": len(repos), "validation": "deferred_not_synced"}
    before = run(["git", "rev-parse", "HEAD"], megavault).stdout.strip()
    already = created = 0
    for repo in repos:
        result = run(
            [
                "python3",
                str(megavault / "megavault.py"),
                "register-github-repo",
                "--owner",
                repo["owner"],
                "--name",
                repo["name"],
                "--remote-url",
                repo["url"],
                "--default-branch",
                repo["default_branch"],
                "--worktree",
                str(projects_dir / repo["name"]),
            ],
            cwd=megavault,
            timeout=60,
        )
        if result.returncode != 0:
            raise AutosyncError("megavault_register_failed")
        if "status=created" in result.stdout:
            created += 1
        else:
            already += 1
    validation = run(["python3", str(megavault / "megavault.py"), "validate"], cwd=megavault, timeout=120)
    if validation.returncode != 0:
        raise AutosyncError("megavault_validate_failed")
    if created:
        run(["git", "add", "megavault.sqlite"], megavault, timeout=30)
        run(["git", "commit", "-m", "Register autosynced GitHub repositories"], megavault, timeout=120)
        push = run(["git", "push", "origin", "master"], megavault, timeout=240)
        if push.returncode != 0:
            raise AutosyncError("megavault_metadata_push_failed")
    after = run(["git", "rev-parse", "HEAD"], megavault).stdout.strip()
    return {"already_registered": already, "newly_registered": created, "deferred": 0, "validation": "PASS", "commit_changed": str(before != after)}


def command_run(args: argparse.Namespace) -> int:
    projects_dir = Path(args.projects_dir).expanduser()
    state_dir = Path(args.state_dir).expanduser()
    megavault = Path(args.megavault).expanduser()
    with ExclusiveLock(state_dir / "autosync.lock"):
        repos = [{**repo, "owner": args.owner} for repo in github_repos(args.owner)]
        ghorg_result = run_ghorg(args.owner, projects_dir, dry_run=args.dry_run)
        summary = summarize_ghorg_output((ghorg_result.stdout or "") + "\n" + (ghorg_result.stderr or ""))
        registration = register_in_megavault(megavault, projects_dir, repos, dry_run=args.dry_run)
    payload = {
        "status": "ok" if ghorg_result.returncode == 0 else "ghorg_failed",
        "owner": args.owner,
        "discovered": len(repos),
        **summary,
        "megavault": registration,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if ghorg_result.returncode == 0 else 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely autosync gernalix GitHub repos with ghorg.")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS_DIR))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--megavault", default=str(DEFAULT_MEGAVAULT))
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run")
    run_p.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BlockingIOError:
        print(json.dumps({"status": "locked"}, sort_keys=True))
        return 0
    except AutosyncError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
