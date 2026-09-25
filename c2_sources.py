#!/usr/bin/env python3
"""Compatibility adapter for the C2 source registry."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from codex_monitor.capsules.source_registry.implementation import main

if __name__ == "__main__":
    raise SystemExit(main())
