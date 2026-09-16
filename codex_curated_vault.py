#!/usr/bin/env python3
"""Compatibility adapter for curated prompt analysis."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.prompt_analysis.curated_vault_api import main, parse_session, rebuild_indexes, write_session
if __name__ == "__main__":
    raise SystemExit(main())
