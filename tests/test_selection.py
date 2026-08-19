"""Tests for per-kind selection persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from djsync import selection


@pytest.fixture
def selection_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "selection.json"
    monkeypatch.setattr(selection, "SELECTION_PATH", path)
    return path


def test_load_selection_empty(selection_file: Path) -> None:
    assert selection.load_selection("playlists") == set()
    assert selection.load_selection("albums") == set()


def test_save_and_load_playlists(selection_file: Path) -> None:
    selection.save_selection({"p1", "p2"}, kind="playlists")
    assert selection.load_selection("playlists") == {"p1", "p2"}
    assert selection.load_selection("albums") == set()


def test_save_and_load_albums(selection_file: Path) -> None:
    selection.save_selection({"a1", "a2"}, kind="albums")
    assert selection.load_selection("albums") == {"a1", "a2"}
    assert selection.load_selection("playlists") == set()


def test_selection_kinds_are_independent(selection_file: Path) -> None:
    selection.save_selection({"p1"}, kind="playlists")
    selection.save_selection({"a1", "a2"}, kind="albums")
    assert selection.load_selection("playlists") == {"p1"}
    assert selection.load_selection("albums") == {"a1", "a2"}


def test_legacy_selected_key_means_playlists(selection_file: Path) -> None:
    selection_file.write_text(
        json.dumps({"selected": ["legacy1", "legacy2"]}),
        encoding="utf-8",
    )
    assert selection.load_selection("playlists") == {"legacy1", "legacy2"}


def test_save_playlists_clears_legacy_key(selection_file: Path) -> None:
    selection_file.write_text(
        json.dumps({"selected": ["old"]}),
        encoding="utf-8",
    )
    selection.save_selection({"new"}, kind="playlists")
    data = json.loads(selection_file.read_text(encoding="utf-8"))
    assert "selected" not in data
    assert data["selected_ids"] == ["new"]
