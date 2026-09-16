#!/usr/bin/env python3
"""Compatibility adapter for the Kuma push API."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.quota.kuma_api import KumaPushError, build_push_url, main, push
if __name__ == "__main__":
    raise SystemExit(main())
