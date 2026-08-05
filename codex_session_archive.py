#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_MODULE_PATH = Path(__file__).resolve().parent / "src/codex_usage_monitor/sessions/commands.py"
_SPEC = importlib.util.spec_from_file_location("_codex_usage_monitor_session_commands", _MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load session commands from {_MODULE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

for _name in dir(_MODULE):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_MODULE, _name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
