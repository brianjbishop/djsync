"""Build and load the playlist/album scan cache."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import spotipy

from djsync import spotify
from djsync.config import PROJECT_ROOT, get_destination
from djsync.library import album_folder, existing_spotify_ids, playlist_folder
from djsync.web_helpers import compute_status

CACHE_PATH = PROJECT_ROOT / ".djsync_cache.json"


def _playlist_entry(
    playlist: spotify.Playlist,
    tracks: list,
) -> dict[str, Any]:
    """Build one cache entry for a $d playlist."""
    folder = playlist_folder(playlist.name, get_destination().path)
    local_ids = existing_spotify_ids(folder)
    track_ids = {t.id for t in tracks}
    downloaded_count = len(local_ids & track_ids)

    added_dates = [t.added_at for t in tracks if t.added_at]
    last_added = max(added_dates) if added_dates else None

    return {
        "id": playlist.id,
        "name": playlist.name,
        "sigils": sorted(playlist.sigils),
        "track_count": playlist.track_count,
        "spotify_url": f"https://open.spotify.com/playlist/{playlist.id}",
        "spotify_uri": f"spotify:playlist:{playlist.id}",
        "downloaded_count": downloaded_count,
        "status": compute_status(downloaded_count, playlist.track_count),
        "last_added": last_added,
    }


def _album_entry(
    album: spotify.Album,
    tracks: list,
) -> dict[str, Any]:
    """Build one cache entry for a saved album."""
    folder = album_folder(
        album.artists, album.name, get_destination().path_for("albums")
    )
    local_ids = existing_spotify_ids(folder)
    track_ids = {t.id for t in tracks}
    downloaded_count = len(local_ids & track_ids)

    return {
        "id": album.id,
        "name": album.name,
        "artists": list(album.artists),
        "total_tracks": album.total_tracks,
        "spotify_url": album.spotify_url,
        "spotify_uri": album.spotify_uri,
        "downloaded_count": downloaded_count,
        "status": compute_status(downloaded_count, album.total_tracks),
        "added_at": album.added_at,
        "release_date": album.release_date,
    }


def build_cache(client: spotipy.Spotify) -> dict[str, Any]:
    """Scan Spotify + drive and return cache payload."""
    all_playlists = spotify.fetch_playlists(client)
    marked_d = [p for p in all_playlists if "d" in p.sigils]

    playlist_entries: list[dict[str, Any]] = []
    for playlist in marked_d:
        tracks = spotify.fetch_tracks(client, playlist.id)
        playlist_entries.append(_playlist_entry(playlist, tracks))

    album_entries: list[dict[str, Any]] = []
    for album in spotify.fetch_saved_albums(client):
        tracks = spotify.fetch_album_tracks(client, album.id, album_name=album.name)
        album_entries.append(_album_entry(album, tracks))

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "playlists": playlist_entries,
        "albums": album_entries,
    }


def save_cache(data: dict[str, Any]) -> None:
    """Write cache to disk."""
    CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_cache() -> dict[str, Any] | None:
    """Load cache from disk, or None if missing/invalid."""
    if not CACHE_PATH.is_file():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data.get("playlists"), list):
        return None
    if "albums" not in data:
        data["albums"] = []
    return data


def get_or_build_cache(client: spotipy.Spotify, *, refresh: bool = False) -> dict[str, Any]:
    """Return cached data, building it first if needed."""
    if not refresh:
        cached = load_cache()
        if cached is not None:
            return cached
    data = build_cache(client)
    save_cache(data)
    return data
