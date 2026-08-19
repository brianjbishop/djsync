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
        playlists_dir="playlists",
        albums_dir="albums",
    )
    assert dest.path == tmp_path / "MyDrive" / "dj" / "playlists"
    assert dest.path_for("playlists") == tmp_path / "MyDrive" / "dj" / "playlists"
    assert dest.path_for("albums") == tmp_path / "MyDrive" / "dj" / "albums"


def test_destination_path_for_kind(tmp_path: Path) -> None:
    dest = Destination(
        drive=tmp_path / "drive",
        library_root="music",
        playlists_dir="lists",
        albums_dir="records",
    )
    assert dest.path_for("playlists") == tmp_path / "drive" / "music" / "lists"
    assert dest.path_for("albums") == tmp_path / "drive" / "music" / "records"


def test_destination_exists_when_collection_dir_present(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    collection = drive / "dj" / "playlists"
    collection.mkdir(parents=True)

    dest = Destination(
        drive=drive,
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    assert dest.exists is True


def test_destination_exists_false_when_missing(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()

    dest = Destination(
        drive=drive,
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    assert dest.exists is False


def test_destination_mounted_requires_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()

    dest = Destination(
        drive=drive,
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )

    monkeypatch.setattr(os.path, "ismount", lambda p: p == drive)
    assert dest.mounted is True

    monkeypatch.setattr(os.path, "ismount", lambda _p: False)
    assert dest.mounted is False


def test_destination_mounted_false_when_drive_missing(tmp_path: Path) -> None:
    dest = Destination(
        drive=tmp_path / "missing",
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    assert dest.mounted is False


def test_destination_free_bytes_when_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    dest = Destination(
        drive=drive,
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )

    monkeypatch.setattr(os.path, "ismount", lambda p: p == drive)

    class Usage:
        free = 5 * 1024 * 1024 * 1024

    with patch("djsync.config.shutil.disk_usage", return_value=Usage()):
        assert dest.free_bytes() == 5 * 1024 * 1024 * 1024


def test_destination_free_bytes_none_when_unmounted(tmp_path: Path) -> None:
    dest = Destination(
        drive=tmp_path / "missing",
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
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
    monkeypatch.setenv("DJSYNC_PLAYLISTS_DIR", "lists")
    monkeypatch.setenv("DJSYNC_ALBUMS_DIR", "records")
    monkeypatch.delenv("DJSYNC_COLLECTION", raising=False)

    dest = get_destination()
    assert dest.drive == drive
    assert dest.library_root == "library"
    assert dest.playlists_dir == "lists"
    assert dest.albums_dir == "records"
    assert dest.path == drive / "library" / "lists"
    assert dest.path_for("albums") == drive / "library" / "records"


def test_get_destination_legacy_collection_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "EXT"
    monkeypatch.setenv("DJSYNC_DRIVE", str(drive))
    monkeypatch.setenv("DJSYNC_LIBRARY_ROOT", "dj")
    monkeypatch.setenv("DJSYNC_COLLECTION", "legacy-playlists")
    monkeypatch.delenv("DJSYNC_PLAYLISTS_DIR", raising=False)

    dest = get_destination()
    assert dest.playlists_dir == "legacy-playlists"
    assert dest.path == drive / "dj" / "legacy-playlists"


def test_playlists_dir_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drive = tmp_path / "BRIANB"
    monkeypatch.setenv("DJSYNC_DRIVE", str(drive))
    monkeypatch.setenv("DJSYNC_LIBRARY_ROOT", "dj")
    monkeypatch.setenv("DJSYNC_PLAYLISTS_DIR", "playlists")
    monkeypatch.delenv("DJSYNC_COLLECTION", raising=False)

    import djsync.config as config

    assert config.PLAYLISTS_DIR == drive / "dj" / "playlists"
