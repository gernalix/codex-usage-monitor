#!/usr/bin/env python3
"""Compatibility adapter for incremental task-cost analysis."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.prompt_analysis.incremental_api import ensure_state_schema, incremental_update, main
if __name__ == "__main__":
    raise SystemExit(main())
