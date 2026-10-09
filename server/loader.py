"""Canonical table loader for the server.

Wraps the existing scripts/table_input.py so server modules get the same
canonical {columns, rows, meta} shape that the rest of the skill uses.

This module is intentionally thin; the heavy lifting lives in the
shared scripts/table_input.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make scripts/ importable when running this file from the skill root.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from table_input import load_table_file  # type: ignore[import-not-found]  # noqa: E402


def load(path: Path | str) -> dict:
    """Load xlsx/csv/json into canonical {columns, rows, meta}."""
    return load_table_file(Path(path))