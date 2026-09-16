#!/usr/bin/env python3
"""Compatibility adapter for structured chat-dump publishing."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.publishing.chat_dump_api import append_rollout, main, source_key
if __name__ == "__main__":
    raise SystemExit(main())
