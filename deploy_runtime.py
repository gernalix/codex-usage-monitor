#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import py_compile
import shutil
import subprocess
import tempfile


DEFAULT_RUNTIME_ROOT = Path.home() / ".local/lib/codex-usage-monitor"
RUNTIME_FILES = (
    "codex_usage_publisher.py",
    "codex_usage_publisher_legacy.py",
    "codex_session_archive.py",
    "codex_usage_monitor.py",
)


class DeployError(RuntimeError):
    pass


def run(cmd: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)


def git_stdout(args: list[str], cwd: Path) -> str:
    result = run(["git", *args], cwd)
    if result.returncode != 0:
        raise DeployError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def assert_clean_synced(source: Path) -> str:
    status = run(["git", "status", "--porcelain"], source)
    if status.returncode != 0:
        raise DeployError("git status failed")
    if status.stdout.strip():
        raise DeployError("worktree dirty")
    upstream = git_stdout(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], source)
    head = git_stdout(["rev-parse", "HEAD"], source)
    upstream_sha = git_stdout(["rev-parse", upstream], source)
    if head != upstream_sha:
        raise DeployError("HEAD is not synchronized with upstream")
    return head


def compile_release(release: Path) -> None:
    for name in RUNTIME_FILES:
        path = release / name
        if not path.is_file():
            raise DeployError(f"missing runtime file: {name}")
        py_compile.compile(str(path), doraise=True)


def atomic_switch(current: Path, release: Path) -> None:
    tmp_link = current.with_name(f".{current.name}.tmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(release, target_is_directory=True)
    tmp_link.replace(current)


def deploy(source: Path, runtime_root: Path) -> dict[str, str]:
    source = source.resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    commit = assert_clean_synced(source)
    releases = runtime_root / "releases"
    release = releases / commit
    if release.exists():
        compile_release(release)
    else:
        with tempfile.TemporaryDirectory(prefix=f".{commit}.", dir=str(releases.parent if releases.exists() else runtime_root.parent)) as tmp:
            staging = Path(tmp) / commit
            staging.mkdir(parents=True)
            for name in RUNTIME_FILES:
                src = source / name
                if not src.is_file():
                    raise DeployError(f"missing source file: {name}")
                shutil.copy2(src, staging / name)
            (staging / "manifest.json").write_text(json.dumps({"commit": commit, "source": str(source)}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            compile_release(staging)
            releases.mkdir(parents=True, exist_ok=True)
            staging.replace(release)
    atomic_switch(runtime_root / "current", release)
    return {"commit": commit, "release": str(release), "current": str((runtime_root / "current").resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy codex-usage-monitor publisher runtime")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    args = parser.parse_args(argv)
    try:
        result = deploy(Path(args.source), Path(args.runtime_root).expanduser())
    except (DeployError, py_compile.PyCompileError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 75
    print(json.dumps({"status": "deployed", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
