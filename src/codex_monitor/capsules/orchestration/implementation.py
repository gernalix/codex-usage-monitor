from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any

DEFAULT_ROADMAP_REPO = Path.home() / "projects/codex-roadmap"
DEFAULT_ROADMAP_DB = DEFAULT_ROADMAP_REPO / "roadmap.sqlite"
DEFAULT_HISTORY_DB = Path.home() / ".local/share/prompt-history/prompt_history.sqlite"


class C2OrchestratorError(RuntimeError):
    pass


def _connect_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise C2OrchestratorError(f"missing database: {resolved}")
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
