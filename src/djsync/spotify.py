"""Spotify API client helpers for reading playlists, albums, and tracks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from djsync import sigils
from djsync.models import Track

__all__ = [
    "Track",
    "Playlist",
    "Album",
    "RateLimitedError",
    "format_rate_limit_message",
    "call_spotify",
    "get_client",
    "fetch_playlists",
    "fetch_tracks",
    "fetch_saved_albums",
    "fetch_album_tracks",
]

SCOPE = (
    "playlist-read-private playlist-read-collaborative user-library-read"
)
CACHE_PATH = ".cache"

T = TypeVar("T")


class RateLimitedError(Exception):
    """Raised when Spotify returns HTTP 429 for this application."""

    def __init__(self, retry_after_seconds: int, *, message: str | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message or format_rate_limit_message(retry_after_seconds))


def format_rate_limit_message(retry_after_seconds: int) -> str:
    """Return a human-readable rate-limit message for CLI and web UI."""
    if retry_after_seconds >= 3600:
        wait = f"~{retry_after_seconds / 3600:.1f} hours"
    elif retry_after_seconds >= 60:
        wait = f"~{retry_after_seconds // 60} minutes"
    else:
        wait = f"~{retry_after_seconds} seconds"
    return (
        f"Spotify rate limit reached for this application. "
        f"Retry after {wait} ({retry_after_seconds} s). "
        "This lockout is per-application, not per-user."
    )


def _parse_retry_after(headers: Any) -> int:
    if not headers:
        return 0
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def call_spotify(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Call a Spotipy method and translate HTTP 429 into RateLimitedError."""
    return _spotify_call(fn, *args, **kwargs)


def _parse_429_reason(exc: SpotifyException) -> str:
    """Best-effort parse of Spotify's 429 reason (e.g. QUOTA_EXCEEDED)."""
    for candidate in (getattr(exc, "msg", None), str(exc)):
        if not candidate:
            continue
        text = str(candidate)
        if "QUOTA_EXCEEDED" in text:
            return "QUOTA_EXCEEDED"
        if "RATE_LIMIT" in text.upper():
            return "RATE_LIMITED"
    return "QUOTA_EXCEEDED"


def _spotify_call(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Call a Spotipy method through the quota ledger."""
    from djsync import quota

    quota.check_can_spend(1)
    try:
        result = fn(*args, **kwargs)
    except SpotifyException as exc:
        if exc.http_status == 429:
            retry_after = _parse_retry_after(exc.headers)
            reason = _parse_429_reason(exc)
            quota.record_429(reason, retry_after)
            raise RateLimitedError(retry_after) from exc
        raise
    quota.record_request()
    return result


@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    track_count: int
    sigils: frozenset[str]
    snapshot_id: str = ""


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    artists: tuple[str, ...]
    total_tracks: int
    release_date: str
    added_at: str
    spotify_url: str
    spotify_uri: str


def get_client() -> spotipy.Spotify:
    """Return an authenticated Spotify client."""
    from djsync.config import load_config

    config = load_config()
    auth_manager = SpotifyOAuth(
        client_id=config["SPOTIFY_CLIENT_ID"],
        client_secret=config["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=config["SPOTIFY_REDIRECT_URI"],
        scope=SCOPE,
        cache_path=CACHE_PATH,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def _track_total(item: dict) -> int:
    """Return a playlist's track count.

    Spotify renamed the simplified playlist object's ``tracks`` field to
    ``items``; both carry ``{"href": ..., "total": N}``. Accept either so the
    count survives the API changing back or serving mixed shapes.
    """
    for key in ("items", "tracks"):
        value = item.get(key)
        if isinstance(value, dict) and "total" in value:
            return value["total"] or 0
    return 0


def fetch_playlists(client: spotipy.Spotify) -> list[Playlist]:
    """Return all of the current user's playlists, fully paginated."""
    playlists: list[Playlist] = []
    seen_ids: set[str] = set()
    offset = 0
    limit = 50

    while True:
        page = _spotify_call(client.current_user_playlists, limit=limit, offset=offset)
        items = page.get("items") or []

        for item in items:
            if item is None:
                continue
            playlist_id = item.get("id")
            # Offset pagination over a large library returns some playlists on
            # more than one page, so the same id can arrive twice. Keep the
            # first sighting; a duplicate would otherwise become a second
            # folder competing for the same name on disk.
            if playlist_id is None or playlist_id in seen_ids:
                continue
            seen_ids.add(playlist_id)
            name = item.get("name") or ""
            playlists.append(
                Playlist(
                    id=playlist_id,
                    name=name,
                    track_count=_track_total(item),
                    sigils=frozenset(sigils.parse_sigils(name)),
                    snapshot_id=item.get("snapshot_id") or "",
                )
            )

        if not page.get("next"):
            break
        offset += limit

    return playlists


def fetch_tracks(client: spotipy.Spotify, playlist_id: str) -> list[Track]:
    """Return all tracks in a playlist, fully paginated."""
    tracks: list[Track] = []
    offset = 0
    limit = 100

    while True:
        page = _spotify_call(
            client.playlist_tracks, playlist_id, limit=limit, offset=offset
        )
        items = page.get("items") or []

        for item in items:
            if item is None:
                continue
            # Spotify renamed the playlist-item payload from "track" to "item"
            # (playlists can hold episodes too). Accept either so this keeps
            # working whichever shape the API serves.
            track = item.get("item") or item.get("track")
            if track is None:
                continue

            track_id = track.get("id")
            if track_id is None:
                continue

            artists = tuple(
                artist["name"]
                for artist in (track.get("artists") or [])
                if artist is not None and artist.get("name")
            )
            artist_ids = tuple(
                artist["id"]
                for artist in (track.get("artists") or [])
                if artist is not None and artist.get("id")
            )
            album = (track.get("album") or {}).get("name") or ""
            external_ids = track.get("external_ids") or {}
            isrc = external_ids.get("isrc")
            added_at = item.get("added_at")

            tracks.append(
                Track(
                    id=track_id,
                    name=track.get("name") or "",
                    artists=artists,
                    album=album,
                    duration_ms=track.get("duration_ms") or 0,
                    isrc=isrc,
                    added_at=added_at,
                    artist_ids=artist_ids,
                    explicit=bool(track.get("explicit")),
                )
            )

        if not page.get("next"):
            break
        offset += limit

    return tracks


def fetch_saved_albums(client: spotipy.Spotify) -> list[Album]:
    """Return the user's saved albums, fully paginated and deduped by id."""
    albums: list[Album] = []
    seen_ids: set[str] = set()
    offset = 0
    limit = 50

    while True:
        page = _spotify_call(client.current_user_saved_albums, limit=limit, offset=offset)
        items = page.get("items") or []

        for item in items:
            if item is None:
                continue
            album_obj = item.get("album") or item.get("item")
            if album_obj is None:
                continue
            album_id = album_obj.get("id")
            if album_id is None or album_id in seen_ids:
                continue
            seen_ids.add(album_id)

            artists = tuple(
                artist["name"]
                for artist in (album_obj.get("artists") or [])
                if artist is not None and artist.get("name")
            )
            external_urls = album_obj.get("external_urls") or {}
            spotify_url = external_urls.get("spotify") or (
                f"https://open.spotify.com/album/{album_id}"
            )
            spotify_uri = album_obj.get("uri") or f"spotify:album:{album_id}"

            albums.append(
                Album(
                    id=album_id,
                    name=album_obj.get("name") or "",
                    artists=artists,
                    total_tracks=album_obj.get("total_tracks") or 0,
                    release_date=album_obj.get("release_date") or "",
                    added_at=item.get("added_at") or "",
                    spotify_url=spotify_url,
                    spotify_uri=spotify_uri,
                )
            )

        if not page.get("next"):
            break
        offset += limit

    return albums


def fetch_album_tracks(
    client: spotipy.Spotify,
    album_id: str,
    *,
    album_name: str,
) -> list[Track]:
    """Return all tracks on an album, fully paginated."""
    tracks: list[Track] = []
    offset = 0
    limit = 50

    while True:
        page = _spotify_call(client.album_tracks, album_id, limit=limit, offset=offset)
        items = page.get("items") or []

        for track in items:
            if track is None:
                continue
            track_id = track.get("id")
            if track_id is None:
                continue

            artists = tuple(
                artist["name"]
                for artist in (track.get("artists") or [])
                if artist is not None and artist.get("name")
            )
            artist_ids = tuple(
                artist["id"]
                for artist in (track.get("artists") or [])
                if artist is not None and artist.get("id")
            )
            external_ids = track.get("external_ids") or {}
            isrc = external_ids.get("isrc")

            tracks.append(
                Track(
                    id=track_id,
                    name=track.get("name") or "",
                    artists=artists,
                    album=album_name,
                    duration_ms=track.get("duration_ms") or 0,
                    isrc=isrc,
                    track_number=track.get("track_number") or 1,
                    disc_number=track.get("disc_number") or 1,
                    explicit=bool(track.get("explicit")),
                )
            )

        if not page.get("next"):
            break
        offset += limit

    return tracks
