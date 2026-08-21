"""Build and load the playlist/album scan cache."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import spotipy

from djsync import spotify
from djsync import config as app_config
from djsync.config import PROJECT_ROOT, SYNC_ALBUMS, get_destination
from djsync.library import album_folder, existing_spotify_ids, playlist_folder
from djsync.models import Track
from djsync.web_helpers import compute_status

CACHE_PATH = PROJECT_ROOT / ".djsync_cache.json"
REFETCH_WARN_THRESHOLD = 25

CollectionStatus = Literal["ok", "never_fetched", "rate_limited", "error"]
COLLECTION_NAMES = ("playlists", "albums")

logger = logging.getLogger(__name__)

LogCallback = Callable[[str], None]

REFRESH_FIRST_MSG = (
    "No cached library data for this playlist. "
    "Run `djsync refresh` first to fetch from Spotify."
)


def _serialize_tracks(tracks: list[Track]) -> list[dict[str, Any]]:
    return [
        {
            "id": track.id,
            "name": track.name,
            "artists": list(track.artists),
            "album": track.album,
            "duration_ms": track.duration_ms,
            "isrc": track.isrc,
            "added_at": track.added_at,
            "artist_ids": list(track.artist_ids),
            "explicit": track.explicit,
        }
        for track in tracks
    ]


def _deserialize_tracks(cached: list[dict[str, Any]]) -> list[Track]:
    return [
        Track(
            id=str(entry["id"]),
            name=entry.get("name") or "",
            artists=tuple(entry.get("artists") or ()),
            album=entry.get("album") or "",
            duration_ms=int(entry.get("duration_ms") or 0),
            isrc=entry.get("isrc"),
            added_at=entry.get("added_at"),
            artist_ids=tuple(entry.get("artist_ids") or ()),
            explicit=bool(entry.get("explicit")),
        )
        for entry in cached
    ]


def _serialize_album_tracks(tracks: list[Track]) -> list[dict[str, Any]]:
    return [
        {
            "id": track.id,
            "name": track.name,
            "artists": list(track.artists),
            "album": track.album,
            "duration_ms": track.duration_ms,
            "isrc": track.isrc,
            "track_number": track.track_number,
            "disc_number": track.disc_number,
            "artist_ids": list(track.artist_ids),
            "explicit": track.explicit,
        }
        for track in tracks
    ]


def _deserialize_album_tracks(cached: list[dict[str, Any]], *, album_name: str) -> list[Track]:
    return [
        Track(
            id=str(entry["id"]),
            name=entry.get("name") or "",
            artists=tuple(entry.get("artists") or ()),
            album=entry.get("album") or album_name,
            duration_ms=int(entry.get("duration_ms") or 0),
            isrc=entry.get("isrc"),
            track_number=int(entry.get("track_number") or 1),
            disc_number=int(entry.get("disc_number") or 1),
            artist_ids=tuple(entry.get("artist_ids") or ()),
            explicit=bool(entry.get("explicit")),
        )
        for entry in cached
    ]


def _empty_collections() -> dict[str, dict[str, Any]]:
    return {
        name: {"status": "never_fetched"}
        for name in COLLECTION_NAMES
    }


def _ensure_collections(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    collections = data.get("collections")
    if not isinstance(collections, dict):
        collections = {}
        data["collections"] = collections
    for name in COLLECTION_NAMES:
        if name not in collections or not isinstance(collections[name], dict):
            rows = data.get(name) or []
            collections[name] = {"status": "ok" if rows else "never_fetched"}
    return collections


def collection_status(data: dict[str, Any] | None, collection: str) -> dict[str, Any]:
    """Return the stored status payload for a collection."""
    if data is None:
        return {"status": "never_fetched"}
    collections = _ensure_collections(data)
    entry = dict(collections.get(collection) or {"status": "never_fetched"})
    status = entry.get("status")
    if status not in ("ok", "never_fetched", "rate_limited", "error"):
        rows = data.get(collection) or []
        entry["status"] = "ok" if rows else "never_fetched"
    return entry


def collection_api_fields(data: dict[str, Any] | None, collection: str) -> dict[str, Any]:
    """Build top-level API fields for a collection's fetch state."""
    entry = collection_status(data, collection)
    payload: dict[str, Any] = {"status": entry.get("status", "never_fetched")}
    if entry.get("retry_after_seconds") is not None:
        payload["retry_after_seconds"] = entry["retry_after_seconds"]
    if entry.get("reset_at") is not None:
        payload["reset_at"] = entry["reset_at"]
    if entry.get("error") is not None:
        payload["error"] = entry["error"]
    return payload


def _reset_at_iso(retry_after_seconds: int) -> str:
    reset = datetime.now(UTC) + timedelta(seconds=max(0, retry_after_seconds))
    return reset.isoformat()


def record_collection_rate_limit(
    data: dict[str, Any],
    collection: str,
    exc: spotify.RateLimitedError,
) -> None:
    """Record a rate-limit state for *collection* without discarding cached rows."""
    collections = _ensure_collections(data)
    collections[collection] = {
        "status": "rate_limited",
        "retry_after_seconds": exc.retry_after_seconds,
        "reset_at": _reset_at_iso(exc.retry_after_seconds),
        "error": str(exc),
    }
    data["timestamp"] = datetime.now(UTC).isoformat()


def record_collection_ok(data: dict[str, Any], collection: str) -> None:
    """Mark a collection as successfully fetched."""
    collections = _ensure_collections(data)
    collections[collection] = {"status": "ok"}


def record_collection_error(data: dict[str, Any], collection: str, message: str) -> None:
    """Record a non-rate-limit failure for *collection*."""
    collections = _ensure_collections(data)
    collections[collection] = {"status": "error", "error": message}
    data["timestamp"] = datetime.now(UTC).isoformat()


def playlist_from_entry(entry: dict[str, Any]) -> spotify.Playlist:
    """Rebuild a Playlist from a cached playlist row."""
    return spotify.Playlist(
        id=entry["id"],
        name=entry["name"],
        track_count=entry["track_count"],
        sigils=frozenset(entry.get("sigils") or []),
        snapshot_id=entry.get("snapshot_id") or "",
    )


def album_from_entry(entry: dict[str, Any]) -> spotify.Album:
    """Rebuild an Album from a cached album row."""
    return spotify.Album(
        id=entry["id"],
        name=entry["name"],
        artists=tuple(entry.get("artists") or []),
        total_tracks=entry["total_tracks"],
        release_date=entry.get("release_date") or "",
        added_at=entry.get("added_at") or "",
        spotify_url=entry.get("spotify_url")
        or f"https://open.spotify.com/album/{entry['id']}",
        spotify_uri=entry.get("spotify_uri")
        or f"spotify:album:{entry['id']}",
    )


def find_d_playlist_by_name(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Return the cached $d playlist entry matching *name*, if any."""
    matches = [
        entry
        for entry in data.get("playlists") or []
        if entry.get("name") == name and "d" in (entry.get("sigils") or [])
    ]
    if not matches:
        return None
    if len(matches) > 1:
        ids = ", ".join(entry["id"] for entry in matches)
        raise ValueError(f'Ambiguous playlist name "{name}" ({len(matches)} matches: {ids}).')
    return matches[0]


def cached_playlist_tracks(entry: dict[str, Any]) -> list[Track] | None:
    """Return cached tracks for a playlist entry, or None if missing."""
    raw = entry.get("tracks")
    if not raw:
        return None
    return _deserialize_tracks(raw)


def playlist_entry_by_id(
    data: dict[str, Any],
    playlist_id: str,
) -> dict[str, Any] | None:
    """Return a playlist row from scanned cache or catalog metadata."""
    for entry in data.get("playlists") or []:
        if entry.get("id") == playlist_id:
            return entry
    for entry in data.get("playlist_catalog") or []:
        if entry.get("id") == playlist_id:
            return _catalog_only_row(entry)
    return None


def _catalog_only_row(catalog_entry: dict[str, Any]) -> dict[str, Any]:
    playlist_id = catalog_entry["id"]
    return {
        "id": playlist_id,
        "name": catalog_entry["name"],
        "sigils": list(catalog_entry.get("sigils") or []),
        "track_count": catalog_entry.get("track_count", 0),
        "snapshot_id": catalog_entry.get("snapshot_id") or "",
        "spotify_url": f"https://open.spotify.com/playlist/{playlist_id}",
        "spotify_uri": f"spotify:playlist:{playlist_id}",
        "downloaded_count": None,
        "status": "not_scanned",
        "last_added": None,
        "scanned": False,
    }


def catalog_playlists_for_ui(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build UI playlist rows from catalog metadata plus any scanned entries."""
    scanned_by_id = {
        entry["id"]: entry for entry in data.get("playlists") or []
    }
    catalog = data.get("playlist_catalog") or []

    if catalog:
        rows: list[dict[str, Any]] = []
        for entry in catalog:
            scanned = scanned_by_id.get(entry["id"])
            if scanned:
                row = {key: value for key, value in scanned.items() if key != "tracks"}
                row["scanned"] = True
            else:
                row = _catalog_only_row(entry)
            rows.append(row)
        return rows

    rows = []
    for entry in scanned_by_id.values():
        row = {key: value for key, value in entry.items() if key != "tracks"}
        row["scanned"] = True
        rows.append(row)
    return rows


def store_playlist_tracks(
    data: dict[str, Any],
    playlist: spotify.Playlist,
    tracks: list[Track],
) -> dict[str, Any]:
    """Insert or update a scanned playlist entry in *data*."""
    entry = _playlist_entry(playlist, tracks)
    playlists = list(data.get("playlists") or [])
    replaced = False
    for index, existing in enumerate(playlists):
        if existing.get("id") == playlist.id:
            playlists[index] = entry
            replaced = True
            break
    if not replaced:
        playlists.append(entry)
    data["playlists"] = playlists
    return entry


def ensure_playlist_tracks(
    client: spotipy.Spotify,
    data: dict[str, Any],
    entry: dict[str, Any],
) -> list[Track]:
    """Return cached tracks, fetching and storing them on demand when missing."""
    tracks = cached_playlist_tracks(entry)
    if tracks is not None:
        return tracks
    playlist = playlist_from_entry(entry)
    tracks = spotify.fetch_tracks(client, playlist.id)
    store_playlist_tracks(data, playlist, tracks)
    return tracks


def cached_album_tracks(data: dict[str, Any], album_id: str, *, album_name: str) -> list[Track] | None:
    """Return cached album tracks by album id, or None if missing."""
    raw = (data.get("album_tracks") or {}).get(album_id)
    if not raw:
        return None
    return _deserialize_album_tracks(raw, album_name=album_name)


class CacheDataError(Exception):
    """Raised when sync cannot proceed without a warm cache or refresh."""


def resolve_playlist_for_sync(
    client: spotipy.Spotify,
    name: str,
    *,
    refresh: bool = False,
    cached: dict[str, Any] | None = None,
) -> tuple[spotify.Playlist, list[Track]]:
    """Resolve a $d playlist and its tracks, preferring cache unless *refresh*."""
    data = cached if cached is not None else load_cache()
    if not refresh:
        if data is None:
            raise CacheDataError(REFRESH_FIRST_MSG)
        entry = find_d_playlist_by_name(data, name)
        if entry is None:
            raise CacheDataError(
                f'No cached $d playlist named "{name}". '
                "Run `djsync refresh` first, or pass --refresh to fetch live."
            )
        tracks = cached_playlist_tracks(entry)
        if tracks is None:
            raise CacheDataError(
                f'Cached playlist "{name}" has no tracks. '
                "Run `djsync refresh` first, or pass --refresh to fetch live."
            )
        return playlist_from_entry(entry), tracks

    playlist_list = spotify.fetch_playlists(client)
    marked = [p for p in playlist_list if "d" in p.sigils]
    matches = [p for p in marked if p.name == name]
    if not matches:
        raise ValueError(
            f'No $d playlist named "{name}" found.\n'
            "Use `djsync playlists` to list available names."
        )
    if len(matches) > 1:
        ids = ", ".join(p.id for p in matches)
        raise ValueError(f'Ambiguous playlist name "{name}" ({len(matches)} matches: {ids}).')
    target = matches[0]

    entry = None
    if data is not None:
        entry = next((e for e in data.get("playlists") or [] if e.get("id") == target.id), None)
    if entry is not None:
        tracks = cached_playlist_tracks(entry)
        if (
            tracks is not None
            and entry.get("snapshot_id") == target.snapshot_id
        ):
            return target, tracks

    tracks = spotify.fetch_tracks(client, target.id)
    return target, tracks


def resolve_album_for_sync(
    client: spotipy.Spotify,
    album: spotify.Album,
    *,
    refresh: bool = False,
    cached: dict[str, Any] | None = None,
) -> list[Track]:
    """Return album tracks, preferring cache unless *refresh*."""
    data = cached if cached is not None else load_cache()
    if not refresh and data is not None:
        tracks = cached_album_tracks(data, album.id, album_name=album.name)
        if tracks is not None:
            return tracks
    if not refresh:
        raise CacheDataError(
            f'No cached tracks for album "{album.name}". '
            "Run `djsync refresh` first."
        )
    return spotify.fetch_album_tracks(client, album.id, album_name=album.name)


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


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def catalog_is_stale(
    data: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    ttl_hours: float | None = None,
) -> bool:
    """Return True when the playlist catalog must be re-listed from Spotify."""
    if not data:
        return True
    catalog = data.get("playlist_catalog") or []
    if not catalog:
        return True
    fetched_raw = data.get("catalog_fetched_at")
    if not fetched_raw:
        return True
    ts = _parse_ts(str(fetched_raw))
    if ts is None:
        return True
    now = now or datetime.now(UTC)
    hours = app_config.CATALOG_TTL_HOURS if ttl_hours is None else ttl_hours
    return now - ts > timedelta(hours=hours)


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


def _refetch_priority(
    playlist: spotify.Playlist,
    cached_entry: dict[str, Any] | None,
) -> tuple[int, str, str]:
    """Sort key: never-fetched first, then changed snapshot_id."""
    never_fetched = cached_entry is None or not cached_entry.get("tracks")
    return (0 if never_fetched else 1, playlist.name.casefold(), playlist.id)


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


def _ordered_scanned_playlists(
    marked_d: list[spotify.Playlist],
    by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep scanned rows aligned with the live $d catalog order."""
    rows: list[dict[str, Any]] = []
    for playlist in marked_d:
        entry = by_id.get(playlist.id)
        if entry is not None and entry.get("tracks"):
            rows.append(entry)
    return rows


def _refresh_local_counts_for_unchanged(
    marked_d: list[spotify.Playlist],
    by_id: dict[str, dict[str, Any]],
    fetched_ids: set[str],
) -> None:
    for playlist in marked_d:
        if playlist.id in fetched_ids:
            continue
        cached = by_id.get(playlist.id)
        if cached is None or not cached.get("tracks"):
            continue
        if _needs_track_refetch(playlist, cached):
            continue
        tracks = _deserialize_tracks(cached["tracks"])
        by_id[playlist.id] = _playlist_entry(playlist, tracks)


def build_cache(
    client: spotipy.Spotify,
    *,
    max_playlists: int | None = None,
    prior: dict[str, Any] | None = None,
    on_log: LogCallback | None = None,
    sync_albums: bool | None = None,
    force_catalog: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scan Spotify + drive and return cache payload.

    Saves to disk after each playlist's tracks are fetched (atomic replace).
    On RateLimitedError, persists completed work, records lockout state, and
    returns cleanly without raising.

    The playlist *catalog* (id/name/sigils/track_count/snapshot_id) is cached
    separately with CATALOG_TTL_HOURS. Re-listing ~1050 playlists costs
    ceil(N/50) requests (~22) and would burn most of the daily budget if done
    every run. Without a fresh catalog we cannot see snapshot_id changes, so a
    playlist edited today may not be noticed until the next catalog refresh —
    that is the correct trade at this budget. Daily catalog refresh is the
    mechanism that eventually catches those edits.
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        else:
            logger.info(msg)

    clock = now or datetime.now(UTC)
    include_albums = SYNC_ALBUMS if sync_albums is None else sync_albums
    prior = dict(prior if prior is not None else load_cache() or {})
    _ensure_collections(prior)
    if "playlists" not in prior:
        prior["playlists"] = []
    if "albums" not in prior:
        prior["albums"] = []
    if "album_tracks" not in prior:
        prior["album_tracks"] = {}
    if "playlist_catalog" not in prior:
        prior["playlist_catalog"] = []

    old_playlists_by_id = {
        entry["id"]: dict(entry) for entry in prior.get("playlists") or []
    }
    album_tracks_cache: dict[str, list[dict[str, Any]]] = dict(
        prior.get("album_tracks") or {}
    )

    refresh_catalog = force_catalog or catalog_is_stale(prior, now=clock)
    if refresh_catalog:
        try:
            all_playlists = spotify.fetch_playlists(client)
        except spotify.RateLimitedError as exc:
            record_collection_rate_limit(prior, "playlists", exc)
            save_cache(prior)
            return prior

        playlist_catalog = [_playlist_catalog_entry(p) for p in all_playlists]
        prior["playlist_catalog"] = playlist_catalog
        prior["catalog_fetched_at"] = clock.isoformat()
        prior["timestamp"] = clock.isoformat()
        save_cache(prior)
        marked_d = [p for p in all_playlists if "d" in p.sigils]
    else:
        all_playlists = playlists_from_catalog(prior)
        marked_d = [p for p in all_playlists if "d" in p.sigils]

    needs_refetch = [
        p for p in marked_d if _needs_track_refetch(p, old_playlists_by_id.get(p.id))
    ]
    needs_refetch.sort(
        key=lambda playlist: _refetch_priority(
            playlist, old_playlists_by_id.get(playlist.id)
        )
    )
    refetch_estimate = len(needs_refetch)
    if refetch_estimate > REFETCH_WARN_THRESHOLD:
        log(
            f"Cache refresh will fetch tracks for {refetch_estimate} playlists "
            f"(>{REFETCH_WARN_THRESHOLD}); unchanged playlists reuse cached data."
        )

    if max_playlists is not None:
        to_fetch = needs_refetch[: max(0, max_playlists)]
        for playlist in needs_refetch[max(0, max_playlists) :]:
            cached = old_playlists_by_id.get(playlist.id)
            if cached and cached.get("tracks"):
                log(
                    f"Skipping track refetch for {playlist.name!r} "
                    f"(max_playlists limit); using cached tracks."
                )
    else:
        to_fetch = needs_refetch

    playlists_by_id = dict(old_playlists_by_id)
    fetched_ids: set[str] = set()

    for playlist in to_fetch:
        try:
            tracks = spotify.fetch_tracks(client, playlist.id)
        except spotify.RateLimitedError as exc:
            record_collection_rate_limit(prior, "playlists", exc)
            _refresh_local_counts_for_unchanged(marked_d, playlists_by_id, fetched_ids)
            prior["playlists"] = _ordered_scanned_playlists(marked_d, playlists_by_id)
            save_cache(prior)
            return prior

        playlists_by_id[playlist.id] = _playlist_entry(playlist, tracks)
        fetched_ids.add(playlist.id)
        _refresh_local_counts_for_unchanged(marked_d, playlists_by_id, fetched_ids)
        prior["playlists"] = _ordered_scanned_playlists(marked_d, playlists_by_id)
        prior["timestamp"] = datetime.now(UTC).isoformat()
        # Persist after EACH playlist so a lockout never discards completed work.
        save_cache(prior)

    _refresh_local_counts_for_unchanged(marked_d, playlists_by_id, fetched_ids)
    prior["playlists"] = _ordered_scanned_playlists(marked_d, playlists_by_id)
    record_collection_ok(prior, "playlists")

    if include_albums:
        try:
            saved_albums = spotify.fetch_saved_albums(client)
        except spotify.RateLimitedError as exc:
            record_collection_rate_limit(prior, "albums", exc)
            prior["timestamp"] = datetime.now(UTC).isoformat()
            save_cache(prior)
            return prior

        album_entries: list[dict[str, Any]] = []
        for album in saved_albums:
            cached_tracks = album_tracks_cache.get(album.id)
            if cached_tracks is not None:
                tracks = _deserialize_album_tracks(cached_tracks, album_name=album.name)
            else:
                try:
                    tracks = spotify.fetch_album_tracks(
                        client, album.id, album_name=album.name
                    )
                except spotify.RateLimitedError as exc:
                    record_collection_rate_limit(prior, "albums", exc)
                    prior["albums"] = album_entries
                    prior["album_tracks"] = album_tracks_cache
                    prior["timestamp"] = datetime.now(UTC).isoformat()
                    save_cache(prior)
                    return prior
                album_tracks_cache[album.id] = _serialize_album_tracks(tracks)
            album_entries.append(_album_entry(album, tracks))
            prior["albums"] = album_entries
            prior["album_tracks"] = album_tracks_cache
            prior["timestamp"] = datetime.now(UTC).isoformat()
            save_cache(prior)

        prior["albums"] = album_entries
        prior["album_tracks"] = album_tracks_cache
        record_collection_ok(prior, "albums")

    prior["timestamp"] = datetime.now(UTC).isoformat()
    save_cache(prior)
    return prior


def save_cache(data: dict[str, Any]) -> None:
    """Write cache to disk atomically (temp file + os.replace)."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(CACHE_PATH.parent),
        prefix=".djsync_cache.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, CACHE_PATH)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
    _ensure_collections(data)
    return data


def load_cache_or_empty() -> dict[str, Any]:
    """Return cached data or an empty shell with never_fetched collection state."""
    return load_cache() or {
        "timestamp": None,
        "playlist_catalog": [],
        "playlists": [],
        "albums": [],
        "album_tracks": {},
        "collections": _empty_collections(),
    }


def refresh_playlist_catalog(
    client: spotipy.Spotify,
    *,
    prior: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch the playlist list and update catalog metadata in the cache."""
    clock = now or datetime.now(UTC)
    prior = dict(prior if prior is not None else load_cache() or {})
    _ensure_collections(prior)
    try:
        all_playlists = spotify.fetch_playlists(client)
    except spotify.RateLimitedError as exc:
        record_collection_rate_limit(prior, "playlists", exc)
        save_cache(prior)
        raise
    prior["playlist_catalog"] = [_playlist_catalog_entry(p) for p in all_playlists]
    prior["catalog_fetched_at"] = clock.isoformat()
    prior["timestamp"] = clock.isoformat()
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
    force_catalog: bool = False,
    on_log: LogCallback | None = None,
) -> dict[str, Any]:
    """Return cached data, building it first if *refresh* is requested."""
    if not refresh:
        cached = load_cache()
        if cached is not None:
            return cached
        return load_cache_or_empty()
    prior = load_cache() or load_cache_or_empty()
    data = build_cache(
        client,
        max_playlists=max_playlists,
        prior=prior,
        force_catalog=force_catalog,
        on_log=on_log,
    )
    # build_cache already saves incrementally; final save is a no-op safety net.
    save_cache(data)
    return data
