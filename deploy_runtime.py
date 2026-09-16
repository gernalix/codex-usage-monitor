#!/usr/bin/env python3
"""Compatibility adapter for immutable runtime deployment."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.runtime_deploy.api import DeployError, RUNTIME_FILES, assert_clean_synced, deploy, git_stdout, main, run
if __name__ == "__main__":
    raise SystemExit(main())
