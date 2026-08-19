"""Tests for configurable sync destination."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from djsync.config import Destination, get_destination
from djsync.web_helpers import format_size


def test_destination_path_composition(tmp_path: Path) -> None:
    dest = Destination(
        drive=tmp_path / "MyDrive",
        library_root="dj",
        collection="playlists",
    )
    assert dest.path == tmp_path / "MyDrive" / "dj" / "playlists"


def test_destination_path_generalizes_collection(tmp_path: Path) -> None:
    dest = Destination(
        drive=tmp_path / "drive",
        library_root="music",
        collection="albums",
    )
    assert dest.path == tmp_path / "drive" / "music" / "albums"


def test_destination_exists_when_collection_dir_present(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    collection = drive / "dj" / "playlists"
    collection.mkdir(parents=True)

    dest = Destination(drive=drive, library_root="dj", collection="playlists")
    assert dest.exists is True


def test_destination_exists_false_when_missing(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()

    dest = Destination(drive=drive, library_root="dj", collection="playlists")
    assert dest.exists is False


def test_destination_mounted_requires_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()

    dest = Destination(drive=drive, library_root="dj", collection="playlists")

    monkeypatch.setattr(os.path, "ismount", lambda p: p == drive)
    assert dest.mounted is True

    monkeypatch.setattr(os.path, "ismount", lambda _p: False)
    assert dest.mounted is False


def test_destination_mounted_false_when_drive_missing(tmp_path: Path) -> None:
    dest = Destination(
        drive=tmp_path / "missing",
        library_root="dj",
        collection="playlists",
    )
    assert dest.mounted is False


def test_destination_free_bytes_when_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    dest = Destination(drive=drive, library_root="dj", collection="playlists")

    monkeypatch.setattr(os.path, "ismount", lambda p: p == drive)

    class Usage:
        free = 5 * 1024 * 1024 * 1024

    with patch("djsync.config.shutil.disk_usage", return_value=Usage()):
        assert dest.free_bytes() == 5 * 1024 * 1024 * 1024


def test_destination_free_bytes_none_when_unmounted(tmp_path: Path) -> None:
    dest = Destination(
        drive=tmp_path / "missing",
        library_root="dj",
        collection="playlists",
    )
    assert dest.free_bytes() is None


def test_format_size_human_readable() -> None:
    assert format_size(512) == "0.5 KB"
    assert format_size(2 * 1024 * 1024) == "2.0 MB"
    assert format_size(3 * 1024 * 1024 * 1024) == "3.00 GB"


def test_get_destination_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "EXT"
    monkeypatch.setenv("DJSYNC_DRIVE", str(drive))
    monkeypatch.setenv("DJSYNC_LIBRARY_ROOT", "library")
    monkeypatch.setenv("DJSYNC_COLLECTION", "albums")

    dest = get_destination()
    assert dest.drive == drive
    assert dest.library_root == "library"
    assert dest.collection == "albums"
    assert dest.path == drive / "library" / "albums"


def test_playlists_dir_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drive = tmp_path / "BRIANB"
    monkeypatch.setenv("DJSYNC_DRIVE", str(drive))
    monkeypatch.setenv("DJSYNC_LIBRARY_ROOT", "dj")
    monkeypatch.setenv("DJSYNC_COLLECTION", "playlists")

    import djsync.config as config

    assert config.PLAYLISTS_DIR == drive / "dj" / "playlists"
