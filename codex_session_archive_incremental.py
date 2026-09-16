#!/usr/bin/env python3
"""Compatibility adapter for incremental session archiving."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.session_archive.incremental_api import incremental_import, main
if __name__ == "__main__":
    raise SystemExit(main())
