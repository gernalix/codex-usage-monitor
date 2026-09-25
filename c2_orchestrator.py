#!/usr/bin/env python3
"""Compatibility adapter for the C2 orchestration API."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from codex_monitor.capsules.orchestration.implementation import main

if __name__ == "__main__":
    raise SystemExit(main())
