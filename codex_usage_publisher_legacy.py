#!/usr/bin/env python3
"""Compatibility adapter for the legacy publisher entrypoint."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.publishing.legacy_api import main, quota
if __name__ == "__main__":
    raise SystemExit(main())
