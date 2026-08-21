"""Spotify API client helpers for reading playlists, albums, and tracks."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from djsync import sigils
from djsync.config import SPOTIFY_MIN_REQUEST_INTERVAL as _CONFIG_MIN_INTERVAL
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

# When Spotify (or spotipy's Max-Retries path) omits Retry-After, assume a
# conservative lockout. retry_after=0 would clear immediately and is worse than
# no tracking — it creates false confidence for the next run.
DEFAULT_429_RETRY_AFTER = 3600

# Module-level so tests can monkeypatch without reloading config.
SPOTIFY_MIN_REQUEST_INTERVAL = float(_CONFIG_MIN_INTERVAL)
_last_request_at: float | None = None
_monotonic = time.monotonic
_sleep = time.sleep

T = TypeVar("T")

_RETRY_AFTER_RE = re.compile(r"Retry-After[=:\s]+(\d+)", re.IGNORECASE)


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


def _parse_retry_after_from_text(text: str) -> int:
    match = _RETRY_AFTER_RE.search(text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _effective_retry_after(seconds: int) -> int:
    return seconds if seconds > 0 else DEFAULT_429_RETRY_AFTER


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


def _message_looks_like_429(msg: str) -> bool:
    lower = msg.lower()
    return (
        "429" in msg
        or "too many 429" in lower
        or "quota_exceeded" in lower
        or "rate limit" in lower
    )


class _429WarningCapture(logging.Handler):
    """Catch spotipy/urllib3 429 warnings that never become exceptions."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.seen = False
        self.retry_after = 0
        self.reason = "QUOTA_EXCEEDED"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if not _message_looks_like_429(msg):
            return
        self.seen = True
        parsed = _parse_retry_after_from_text(msg)
        if parsed > 0:
            self.retry_after = parsed
        if "QUOTA_EXCEEDED" in msg:
            self.reason = "QUOTA_EXCEEDED"
        elif "RATE_LIMIT" in msg.upper():
            self.reason = "RATE_LIMITED"


def _enforce_min_interval() -> None:
    """Sleep so consecutive Spotify calls respect SPOTIFY_MIN_REQUEST_INTERVAL."""
    global _last_request_at
    interval = float(SPOTIFY_MIN_REQUEST_INTERVAL)
    if interval <= 0:
        return
    now = _monotonic()
    if _last_request_at is not None:
        elapsed = now - _last_request_at
        if elapsed < interval:
            _sleep(interval - elapsed)


def _mark_request_time() -> None:
    global _last_request_at
    _last_request_at = _monotonic()


def _record_and_raise_429(reason: str, retry_after: int) -> None:
    from djsync import quota

    effective = _effective_retry_after(retry_after)
    quota.record_429(reason, effective)
    raise RateLimitedError(effective)


def _spotify_call(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Call a Spotipy method through the quota ledger with pacing."""
    from djsync import quota

    # A full burst window is a pacing signal, not an error: wait it out.
    # check_can_spend still refuses on lockout or exhausted daily budget.
    quota.wait_for_burst_capacity(1)
    quota.check_can_spend(1)
    _enforce_min_interval()

    capture = _429WarningCapture()
    loggers = [
        logging.getLogger("spotipy"),
        logging.getLogger("urllib3"),
        logging.getLogger("urllib3.util.retry"),
        logging.getLogger("requests"),
    ]
    for logger in loggers:
        logger.addHandler(capture)

    try:
        try:
            result = fn(*args, **kwargs)
        except SpotifyException as exc:
            _mark_request_time()
            if exc.http_status == 429:
                retry_after = _parse_retry_after(exc.headers)
                reason = _parse_429_reason(exc)
                _record_and_raise_429(reason, retry_after)
            raise
        except RateLimitedError:
            _mark_request_time()
            raise
        except Exception as exc:
            _mark_request_time()
            # urllib3 MaxRetryError / requests RetryError after internal 429 retries.
            name = type(exc).__name__
            if name in ("RetryError", "MaxRetryError"):
                retry_after = _parse_retry_after_from_text(str(exc))
                _record_and_raise_429("QUOTA_EXCEEDED", retry_after)
            raise
        else:
            _mark_request_time()
            if capture.seen:
                _record_and_raise_429(capture.reason, capture.retry_after)
            quota.record_request()
            return result
    finally:
        for logger in loggers:
            logger.removeHandler(capture)


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
    # Do not let urllib3/spotipy silently retry 429s (they log WARNING and can
    # surface Max-Retries without Retry-After headers). We handle 429 ourselves.
    return spotipy.Spotify(
        auth_manager=auth_manager,
        retries=0,
        status_retries=0,
        status_forcelist=(),
    )


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
