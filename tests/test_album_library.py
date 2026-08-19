"""Tests for album folder naming and track filenames."""

from __future__ import annotations

from pathlib import Path

from djsync.library import album_folder, album_track_filename
from djsync.models import Track


def test_album_folder_artists_and_name() -> None:
    root = Path("/tmp/albums")
    folder = album_folder(("Smino",), "SAD", root)
    assert folder == root / "Smino - SAD"


def test_album_folder_multiple_artists() -> None:
    root = Path("/tmp/albums")
    folder = album_folder(("Artist A", "Artist B"), "Collab Album", root)
    assert folder == root / "Artist A, Artist B - Collab Album"


def test_album_folder_sanitizes_slash() -> None:
    root = Path("/tmp/albums")
    folder = album_folder(("A/B",), "C/D", root)
    assert folder == root / "A-B - C-D"


def test_album_folder_preserves_unicode() -> None:
    root = Path("/tmp/albums")
    folder = album_folder(("Björk",), "Vespertine", root)
    assert folder == root / "Björk - Vespertine"


def test_album_track_filename_single_disc_zero_padded() -> None:
    track = Track(
        id="t1",
        name="Fronto Isley",
        artists=("Smino",),
        album="SAD",
        duration_ms=180_000,
        isrc=None,
        track_number=1,
        disc_number=1,
    )
    assert album_track_filename(track, multi_disc=False) == "01 Fronto Isley.mp3"


def test_album_track_filename_double_digit_track() -> None:
    track = Track(
        id="t1",
        name="Outro",
        artists=("Smino",),
        album="SAD",
        duration_ms=180_000,
        isrc=None,
        track_number=12,
        disc_number=1,
    )
    assert album_track_filename(track, multi_disc=False) == "12 Outro.mp3"


def test_album_track_filename_multi_disc_prefix() -> None:
    track = Track(
        id="t1",
        name="Disc Two Opener",
        artists=("Band",),
        album="Live",
        duration_ms=180_000,
        isrc=None,
        track_number=1,
        disc_number=2,
    )
    assert album_track_filename(track, multi_disc=True) == "2-01 Disc Two Opener.mp3"


def test_album_track_filename_sanitizes_slash_in_title() -> None:
    track = Track(
        id="t1",
        name="AC/DC Tribute",
        artists=("Band",),
        album="Covers",
        duration_ms=180_000,
        isrc=None,
        track_number=3,
        disc_number=1,
    )
    assert album_track_filename(track, multi_disc=False) == "03 AC-DC Tribute.mp3"
