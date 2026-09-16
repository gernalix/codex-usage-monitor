#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> int:
    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src if not env.get("PYTHONPATH") else f"{src}{os.pathsep}{env['PYTHONPATH']}"
    result = subprocess.run(cmd, cwd=ROOT, check=False, env=env)
    return int(result.returncode)


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

    rc = run(test_cmd)
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
