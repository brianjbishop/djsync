"""Persist playlist and album checkbox selections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from djsync.config import PROJECT_ROOT

SELECTION_PATH = PROJECT_ROOT / "selection.json"

SelectionKind = Literal["playlists", "albums"]


def _read_payload() -> dict:
    if not SELECTION_PATH.is_file():
        return {}
    try:
        data = json.loads(SELECTION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_selection(kind: SelectionKind = "playlists") -> set[str]:
    """Return the set of selected ids for *kind*."""
    data = _read_payload()
    if kind == "albums":
        ids = data.get("selected_album_ids")
    else:
        ids = data.get("selected_ids")
        if ids is None:
            # Backwards compatibility with {"selected": [...]}.
            ids = data.get("selected")
    if not isinstance(ids, list):
        return set()
    return {str(i) for i in ids}


def save_selection(selected_ids: set[str], kind: SelectionKind = "playlists") -> None:
    """Persist selected ids for *kind*, preserving the other kind's selection."""
    data = _read_payload()
    key = "selected_album_ids" if kind == "albums" else "selected_ids"
    data[key] = sorted(selected_ids)
    # Drop legacy key once playlists are saved in the new shape.
    data.pop("selected", None)
    SELECTION_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
