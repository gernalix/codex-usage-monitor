#!/usr/bin/env python3
"""Compatibility adapter for the session archive capsule."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.session_archive.api import (
    command_diagnostic_bundle, command_import, command_verify, connect_db,
    import_session, main,
)
if __name__ == "__main__":
    raise SystemExit(main())
