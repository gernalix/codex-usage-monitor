#!/usr/bin/env python3
"""Fedora-local entry point for the read-only Codex activity dashboard."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.quota.live_dashboard import main

if __name__ == "__main__":
    main()
