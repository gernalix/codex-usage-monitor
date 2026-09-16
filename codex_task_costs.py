#!/usr/bin/env python3
"""Compatibility adapter for task-cost analysis."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.prompt_analysis.costs_api import (
    analyze_with_prompts, main, select_prompt_rows, write_outputs,
)
if __name__ == "__main__":
    raise SystemExit(main())
