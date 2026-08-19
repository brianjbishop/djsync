"""Build and load the playlist scan cache."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import spotipy

from djsync import spotify
from djsync.config import PLAYLISTS_DIR, PROJECT_ROOT
from djsync.library import existing_spotify_ids, playlist_folder
from djsync.web_helpers import compute_status

CACHE_PATH = PROJECT_ROOT / ".djsync_cache.json"


def _playlist_entry(
    playlist: spotify.Playlist,
    tracks: list,
) -> dict[str, Any]:
    """Build one cache entry for a $d playlist."""
    folder = playlist_folder(playlist.name, PLAYLISTS_DIR)
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


def build_cache(client: spotipy.Spotify) -> dict[str, Any]:
    """Scan Spotify + drive and return cache payload."""
    all_playlists = spotify.fetch_playlists(client)
    marked_d = [p for p in all_playlists if "d" in p.sigils]

    entries: list[dict[str, Any]] = []
    for playlist in marked_d:
        tracks = spotify.fetch_tracks(client, playlist.id)
        entries.append(_playlist_entry(playlist, tracks))

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "playlists": entries,
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
