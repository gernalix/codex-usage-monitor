#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    remote = upstream.split("/", 1)[0] if "/" in upstream else upstream
    fetched = run(["git", "fetch", remote], source, timeout=180)
    if fetched.returncode != 0:
        raise DeployError(f"git fetch {remote} failed: {(fetched.stderr or fetched.stdout).strip()}")

    head = git_stdout(["rev-parse", "HEAD"], source)
    upstream_sha = git_stdout(["rev-parse", upstream], source)
    if head != upstream_sha:
        raise DeployError("HEAD is not synchronized with upstream")
    return head


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in RUNTIME_FILES:
        path = root / name
        if not path.is_file():
            raise DeployError(f"missing runtime file: {name}")
        hashes[name] = file_sha256(path)
    return hashes


def compile_release(release: Path) -> None:
    for name in RUNTIME_FILES:
        path = release / name
        if not path.is_file():
            raise DeployError(f"missing runtime file: {name}")
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            raise py_compile.PyCompileError(exc, dfile=str(path)) from exc


def write_manifest(release: Path, *, commit: str, source: Path, hashes: dict[str, str]) -> None:
    payload = {
        "commit": commit,
        "source": str(source),
        "files": hashes,
    }
    (release / "manifest.json").write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def verify_release(release: Path, *, commit: str, expected_hashes: dict[str, str]) -> None:
    manifest_path = release / "manifest.json"
    if not manifest_path.is_file():
        raise DeployError(f"release missing manifest: {release}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError(f"invalid release manifest: {release}") from exc
    if manifest.get("commit") != commit:
        raise DeployError(f"release commit mismatch: {release}")
    if manifest.get("files") != expected_hashes:
        raise DeployError(f"release manifest hash mismatch: {release}")
    actual_hashes = runtime_hashes(release)
    if actual_hashes != expected_hashes:
        raise DeployError(f"release file integrity mismatch: {release}")
    compile_release(release)


def make_release_read_only(release: Path) -> None:
    for name in (*RUNTIME_FILES, "manifest.json"):
        path = release / name
        if path.exists():
            path.chmod(0o444)
    release.chmod(0o555)


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
    expected_hashes = runtime_hashes(source)
    releases = runtime_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / commit

    if release.exists():
        verify_release(release, commit=commit, expected_hashes=expected_hashes)
    else:
        with tempfile.TemporaryDirectory(prefix=f".{commit}.", dir=str(runtime_root)) as tmp:
            staging = Path(tmp) / commit
            staging.mkdir(parents=True)
            for name in RUNTIME_FILES:
                shutil.copy2(source / name, staging / name)
            write_manifest(staging, commit=commit, source=source, hashes=expected_hashes)
            verify_release(staging, commit=commit, expected_hashes=expected_hashes)
            make_release_read_only(staging)
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
