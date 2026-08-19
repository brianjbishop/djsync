"""Persist playlist checkbox selection."""

from __future__ import annotations

import json
from pathlib import Path

from djsync.config import PROJECT_ROOT

SELECTION_PATH = PROJECT_ROOT / "selection.json"


def load_selection() -> set[str]:
    """Return the set of selected playlist ids."""
    if not SELECTION_PATH.is_file():
        return set()
    try:
        data = json.loads(SELECTION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    ids = data.get("selected_ids")
    if not isinstance(ids, list):
        return set()
    return {str(i) for i in ids}


def save_selection(selected_ids: set[str]) -> None:
    """Persist selected playlist ids."""
    payload = {"selected_ids": sorted(selected_ids)}
    SELECTION_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
