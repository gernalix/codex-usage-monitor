from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_QUOTA_DB = Path.home() / ".local/share/codex-usage-monitor/codex_usage_monitor.db"
DEFAULT_ARCHIVE_DB = Path.home() / ".local/share/codex-session-archive/index/archive.sqlite"
DEFAULT_HISTORY_DB = Path.home() / ".local/share/prompt-history/prompt_history.sqlite"
DEFAULT_ROADMAP_DB = Path.home() / "projects/codex-roadmap/roadmap.sqlite"
DEFAULT_PUBLISHER_STATE = Path.home() / ".local/state/codex-usage-publisher/github-actions-watch.json"
DEFAULT_CREDENTIAL = Path.home() / ".config/codex-usage-monitor/c2-kuma.env"


class C2HealthError(RuntimeError):
    pass


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
