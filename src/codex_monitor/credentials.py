from __future__ import annotations

import os
from pathlib import Path


def credential_path(name: str, legacy: Path | str) -> Path:
    """Return the systemd credential when present, else the explicit legacy path."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if directory:
        candidate = Path(directory) / name
        if candidate.is_file():
            return candidate
    return Path(legacy).expanduser()
