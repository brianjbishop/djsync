"""Build and load the playlist/album scan cache."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import spotipy

from djsync import spotify
from djsync.config import PROJECT_ROOT, get_destination
from djsync.library import album_folder, existing_spotify_ids, playlist_folder
from djsync.models import Track
from djsync.web_helpers import compute_status

CACHE_PATH = PROJECT_ROOT / ".djsync_cache.json"
REFETCH_WARN_THRESHOLD = 25

logger = logging.getLogger(__name__)

LogCallback = Callable[[str], None]


def _serialize_tracks(tracks: list[Track]) -> list[dict[str, Any]]:
    return [
        {
            "id": track.id,
            "duration_ms": track.duration_ms,
            "added_at": track.added_at,
        }
        for track in tracks
    ]


def _deserialize_tracks(cached: list[dict[str, Any]]) -> list[Track]:
    return [
        Track(
            id=str(entry["id"]),
            name="",
            artists=(),
            album="",
            duration_ms=int(entry.get("duration_ms") or 0),
            isrc=None,
            added_at=entry.get("added_at"),
        )
        for entry in cached
    ]


def _serialize_album_tracks(tracks: list[Track]) -> list[dict[str, Any]]:
    return [
        {
            "id": track.id,
            "duration_ms": track.duration_ms,
            "track_number": track.track_number,
            "disc_number": track.disc_number,
        }
        for track in tracks
    ]


def _deserialize_album_tracks(cached: list[dict[str, Any]], *, album_name: str) -> list[Track]:
    return [
        Track(
            id=str(entry["id"]),
            name="",
            artists=(),
            album=album_name,
            duration_ms=int(entry.get("duration_ms") or 0),
            isrc=None,
            track_number=int(entry.get("track_number") or 1),
            disc_number=int(entry.get("disc_number") or 1),
        )
        for entry in cached
    ]


def _playlist_catalog_entry(playlist: spotify.Playlist) -> dict[str, Any]:
    return {
        "id": playlist.id,
        "name": playlist.name,
        "track_count": playlist.track_count,
        "sigils": sorted(playlist.sigils),
        "snapshot_id": playlist.snapshot_id,
    }


def playlists_from_catalog(data: dict[str, Any]) -> list[spotify.Playlist]:
    """Rebuild Playlist objects from cached catalog metadata."""
    playlists: list[spotify.Playlist] = []
    for entry in data.get("playlist_catalog") or []:
        playlists.append(
            spotify.Playlist(
                id=entry["id"],
                name=entry["name"],
                track_count=entry["track_count"],
                sigils=frozenset(entry.get("sigils") or []),
                snapshot_id=entry.get("snapshot_id") or "",
            )
        )
    return playlists


def _playlist_entry(
    playlist: spotify.Playlist,
    tracks: list[Track],
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
        "snapshot_id": playlist.snapshot_id,
        "tracks": _serialize_tracks(tracks),
        "spotify_url": f"https://open.spotify.com/playlist/{playlist.id}",
        "spotify_uri": f"spotify:playlist:{playlist.id}",
        "downloaded_count": downloaded_count,
        "status": compute_status(downloaded_count, playlist.track_count),
        "last_added": last_added,
    }


def _album_entry(
    album: spotify.Album,
    tracks: list[Track],
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


def _needs_track_refetch(
    playlist: spotify.Playlist,
    cached_entry: dict[str, Any] | None,
) -> bool:
    if cached_entry is None:
        return True
    if cached_entry.get("snapshot_id") != playlist.snapshot_id:
        return True
    if not cached_entry.get("tracks"):
        return True
    return False


def estimate_playlist_refetches(
    marked_d: list[spotify.Playlist],
    prior: dict[str, Any] | None,
) -> int:
    """Return how many $d playlists would trigger fetch_tracks on refresh."""
    old_by_id = {
        entry["id"]: entry for entry in (prior or {}).get("playlists") or []
    }
    return sum(
        1 for playlist in marked_d if _needs_track_refetch(playlist, old_by_id.get(playlist.id))
    )


def build_cache(
    client: spotipy.Spotify,
    *,
    max_playlists: int | None = None,
    prior: dict[str, Any] | None = None,
    on_log: LogCallback | None = None,
) -> dict[str, Any]:
    """Scan Spotify + drive and return cache payload."""
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        else:
            logger.info(msg)

    prior = prior if prior is not None else load_cache()
    old_playlists_by_id = {
        entry["id"]: entry for entry in (prior or {}).get("playlists") or []
    }
    album_tracks_cache: dict[str, list[dict[str, Any]]] = dict(
        (prior or {}).get("album_tracks") or {}
    )

    all_playlists = spotify.fetch_playlists(client)
    playlist_catalog = [_playlist_catalog_entry(p) for p in all_playlists]
    marked_d = [p for p in all_playlists if "d" in p.sigils]

    needs_refetch = [
        p for p in marked_d if _needs_track_refetch(p, old_playlists_by_id.get(p.id))
    ]
    refetch_estimate = len(needs_refetch)
    if refetch_estimate > REFETCH_WARN_THRESHOLD:
        log(
            f"Cache refresh will fetch tracks for {refetch_estimate} playlists "
            f"(>{REFETCH_WARN_THRESHOLD}); unchanged playlists reuse cached data."
        )

    fetch_budget = refetch_estimate if max_playlists is None else max_playlists
    fetch_remaining = fetch_budget

    playlist_entries: list[dict[str, Any]] = []
    for playlist in marked_d:
        cached = old_playlists_by_id.get(playlist.id)
        if _needs_track_refetch(playlist, cached):
            if fetch_remaining > 0:
                tracks = spotify.fetch_tracks(client, playlist.id)
                fetch_remaining -= 1
            elif cached and cached.get("tracks"):
                log(
                    f"Skipping track refetch for {playlist.name!r} "
                    f"(max_playlists limit); using cached tracks."
                )
                tracks = _deserialize_tracks(cached["tracks"])
                playlist = spotify.Playlist(
                    id=playlist.id,
                    name=playlist.name,
                    track_count=playlist.track_count,
                    sigils=playlist.sigils,
                    snapshot_id=cached.get("snapshot_id") or playlist.snapshot_id,
                )
            else:
                tracks = spotify.fetch_tracks(client, playlist.id)
        else:
            tracks = _deserialize_tracks(cached["tracks"])

        playlist_entries.append(_playlist_entry(playlist, tracks))

    album_entries: list[dict[str, Any]] = []
    for album in spotify.fetch_saved_albums(client):
        cached_tracks = album_tracks_cache.get(album.id)
        if cached_tracks is not None:
            tracks = _deserialize_album_tracks(cached_tracks, album_name=album.name)
        else:
            tracks = spotify.fetch_album_tracks(client, album.id, album_name=album.name)
            album_tracks_cache[album.id] = _serialize_album_tracks(tracks)
        album_entries.append(_album_entry(album, tracks))

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "playlist_catalog": playlist_catalog,
        "playlists": playlist_entries,
        "albums": album_entries,
        "album_tracks": album_tracks_cache,
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
    if "album_tracks" not in data:
        data["album_tracks"] = {}
    if "playlist_catalog" not in data:
        data["playlist_catalog"] = []
    return data


def refresh_playlist_catalog(
    client: spotipy.Spotify,
    *,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch the playlist list and update catalog metadata in the cache."""
    prior = dict(prior if prior is not None else load_cache() or {})
    all_playlists = spotify.fetch_playlists(client)
    prior["playlist_catalog"] = [_playlist_catalog_entry(p) for p in all_playlists]
    prior["timestamp"] = datetime.now(UTC).isoformat()
    if "playlists" not in prior:
        prior["playlists"] = []
    if "albums" not in prior:
        prior["albums"] = []
    if "album_tracks" not in prior:
        prior["album_tracks"] = {}
    return prior


def get_or_build_cache(
    client: spotipy.Spotify,
    *,
    refresh: bool = False,
    max_playlists: int | None = None,
    on_log: LogCallback | None = None,
) -> dict[str, Any]:
    """Return cached data, building it first if needed."""
    if not refresh:
        cached = load_cache()
        if cached is not None:
            return cached
    prior = load_cache() if refresh else None
    data = build_cache(
        client,
        max_playlists=max_playlists,
        prior=prior,
        on_log=on_log,
    )
    save_cache(data)
    return data
