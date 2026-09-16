#!/usr/bin/env python3
"""Compatibility adapter for prompt-cost queries."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.prompt_analysis.query_api import main, query_prompt_costs, refresh_prompt_costs, source_fingerprint
if __name__ == "__main__":
    raise SystemExit(main())
