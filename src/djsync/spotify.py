"""Spotify API client helpers for reading playlists and tracks."""

from __future__ import annotations

from dataclasses import dataclass

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from djsync import sigils
from djsync.models import Track

__all__ = ["Track", "Playlist", "get_client", "fetch_playlists", "fetch_tracks"]

SCOPE = "playlist-read-private playlist-read-collaborative"
CACHE_PATH = ".cache"


@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    track_count: int
    sigils: frozenset[str]


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
        page = client.current_user_playlists(limit=limit, offset=offset)
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
        page = client.playlist_tracks(playlist_id, limit=limit, offset=offset)
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
                )
            )

        if not page.get("next"):
            break
        offset += limit

    return tracks
