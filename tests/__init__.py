"""Make targeted unittest module runs work from a source checkout."""

from pathlib import Path
import sys


_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
