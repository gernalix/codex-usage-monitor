#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_MODULE_PATH = Path(__file__).resolve().parent / "src/codex_usage_monitor/usage/commands.py"
_SPEC = importlib.util.spec_from_file_location("_codex_usage_monitor_usage_commands", _MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load usage commands from {_MODULE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

for _name in dir(_MODULE):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_MODULE, _name)


def unified_main(argv: list[str] | None = None) -> int:
    package_root = Path(__file__).resolve().parent / "src"
    sys.path.insert(0, str(package_root))
    from codex_usage_monitor.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in {"usage", "sessions", "status", "verify", "export", "validate-export", "search", "systemd"}:
        raise SystemExit(unified_main(args))
    raise SystemExit(main(args))
