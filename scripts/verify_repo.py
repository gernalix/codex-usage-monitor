#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


RESOURCE_WARNING_MARKERS = (
    "ResourceWarning:",
    "Exception ignored while finalizing database connection",
)


def run(cmd: list[str], *, fail_on_resource_warning: bool = False) -> int:
    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src if not env.get("PYTHONPATH") else f"{src}{os.pathsep}{env['PYTHONPATH']}"

    if not fail_on_resource_warning:
        result = subprocess.run(cmd, cwd=ROOT, check=False, env=env)
        return int(result.returncode)

    process = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    saw_resource_warning = False
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        if any(marker in line for marker in RESOURCE_WARNING_MARKERS):
            saw_resource_warning = True
    returncode = int(process.wait())
    if returncode == 0 and saw_resource_warning:
        print("VERIFY_RESOURCE_WARNING=FAIL", file=sys.stderr)
        return 86
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run codex-usage-monitor verification without creating a venv or requiring pytest."
    )
    parser.add_argument(
        "tests",
        nargs="*",
        help="Optional unittest module names. With none, discover tests/test_*.py.",
    )
    parser.add_argument(
        "--skip-diff-check",
        action="store_true",
        help="Skip git diff --check (useful outside a Git checkout).",
    )
    parser.add_argument(
        "--fail-on-resource-warning",
        action="store_true",
        help="Fail verification when ResourceWarning/unclosed SQLite diagnostics appear.",
    )
    args = parser.parse_args(argv)

    rc = run([sys.executable, "scripts/check_architecture_boundaries.py"])
    if rc != 0:
        return rc

    if args.tests:
        test_cmd = [sys.executable, "-m", "unittest", *args.tests]
    else:
        test_cmd = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
        ]

    strict_resource_warnings = (
        args.fail_on_resource_warning
        or "ResourceWarning" in os.environ.get("PYTHONWARNINGS", "")
    )
    rc = run(test_cmd, fail_on_resource_warning=strict_resource_warnings)
    if rc != 0:
        return rc

    if not args.skip_diff_check:
        rc = run(["git", "diff", "--check"])
        if rc != 0:
            return rc

    print("VERIFY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
