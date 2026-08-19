"""Local playlist/album folder layout and deduplication helpers."""

from __future__ import annotations

from pathlib import Path

from djsync.download import sanitize_filename
from djsync.models import Track
from djsync.tagging import read_spotify_id


def playlist_folder(playlist_name: str, root: Path) -> Path:
    """Return the on-disk folder for a Spotify playlist name."""
    folder_name = playlist_name.replace("/", "-")
    return root / folder_name


def album_folder(artists: tuple[str, ...], album_name: str, root: Path) -> Path:
    """Return the on-disk folder for a saved album."""
    artists_str = ", ".join(artists)
    folder_name = f"{artists_str} - {album_name}".replace("/", "-")
    return root / folder_name


def album_track_filename(track: Track, *, multi_disc: bool) -> str:
    """Return the on-disk filename for a track within an album folder."""
    stem = sanitize_filename(track.name)
    if multi_disc:
        prefix = f"{track.disc_number}-{track.track_number:02d}"
    else:
        prefix = f"{track.track_number:02d}"
    return f"{prefix} {stem}.mp3"


def existing_spotify_ids(folder: Path) -> set[str]:
    """Return SPOTIFY_ID tags found in *.mp3 files under *folder*."""
    if not folder.is_dir():
        return set()

    ids: set[str] = set()
    for path in folder.glob("*.mp3"):
        if path.name.startswith("._"):
            continue
        spotify_id = read_spotify_id(path)
        if spotify_id:
            ids.add(spotify_id)
    return ids
