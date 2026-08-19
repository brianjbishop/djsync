"""Tests for playlist folder paths and local ID scanning."""

from __future__ import annotations

from pathlib import Path

from mutagen.id3 import ID3, TXXX

from djsync.library import existing_spotify_ids, playlist_folder
from djsync.tagging import read_spotify_id, write_tags
from djsync.matcher.candidate import Candidate
from djsync.models import Track


def test_playlist_folder_preserves_unicode_and_spaces() -> None:
    name = " šüñday - $track $ave $et $d "
    root = Path("/tmp/playlists")
    assert playlist_folder(name, root) == root / name.replace("/", "-")


def test_playlist_folder_replaces_slash_only() -> None:
    root = Path("/tmp/playlists")
    assert playlist_folder("a/b", root) == root / "a-b"


def test_existing_spotify_ids_skips_appledouble(tmp_path: Path) -> None:
    track = Track(
        id="abc123",
        name="Test Song",
        artists=("Artist",),
        album="Album",
        duration_ms=180_000,
        isrc=None,
    )
    cand = Candidate(
        video_id="vid",
        url="https://example.com",
        title="Test Song",
        channel="Artist - Topic",
        duration_s=180,
        view_count=0,
        is_art_track=True,
        description="",
    )

    real_mp3 = tmp_path / "Test Song.mp3"
    real_mp3.write_bytes(b"fake")
    write_tags(real_mp3, track, cand)

    appledouble = tmp_path / "._Test Song.mp3"
    tags = ID3()
    tags.add(TXXX(encoding=3, desc="SPOTIFY_ID", text="ghost"))
    tags.save(appledouble)

    assert existing_spotify_ids(tmp_path) == {"abc123"}
    assert read_spotify_id(real_mp3) == "abc123"
