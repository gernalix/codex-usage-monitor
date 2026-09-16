#!/usr/bin/env python3
"""Compatibility adapter for the GitHub Actions watch capsule."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.github_actions.api import notification_message, process_scan, publish_snapshot, semantic_snapshot, main
if __name__ == "__main__":
    raise SystemExit(main())
