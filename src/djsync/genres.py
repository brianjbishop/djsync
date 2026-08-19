"""Fetch and cache playlist genres from Spotify artist metadata."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import spotipy

from djsync.config import PROJECT_ROOT
from djsync import spotify

GENRES_PATH = PROJECT_ROOT / "genres.json"


def load_genres() -> dict[str, str]:
    """Return cached genre strings keyed by playlist id."""
    if not GENRES_PATH.is_file():
        return {}
    try:
        data = json.loads(GENRES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    genres = data.get("genres")
    if not isinstance(genres, dict):
        return {}
    return {str(k): str(v) for k, v in genres.items()}


def save_genres(genres: dict[str, str]) -> None:
    """Persist genre cache."""
    payload = {"genres": genres}
    GENRES_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _batch_artist_genres(
    client: spotipy.Spotify, artist_ids: list[str]
) -> list[str]:
    """Return all genre strings for a batch of artist ids (max 50)."""
    if not artist_ids:
        return []
    result = spotify.call_spotify(client.artists, artist_ids[:50])
    genres: list[str] = []
    for artist in result.get("artists") or []:
        if artist is None:
            continue
        for genre in artist.get("genres") or []:
            if genre:
                genres.append(genre)
    return genres


def dominant_genre(genre_strings: list[str]) -> str | None:
    """Return the most common genre string, or None if empty."""
    if not genre_strings:
        return None
    counts = Counter(genre_strings)
    return counts.most_common(1)[0][0]


def fetch_playlist_genre(
    client: spotipy.Spotify, playlist_id: str
) -> str | None:
    """Fetch tracks, batch artist lookups, return the dominant genre."""
    tracks = spotify.fetch_tracks(client, playlist_id)
    artist_ids: list[str] = []
    seen: set[str] = set()
    for track in tracks:
        for aid in track.artist_ids:
            if aid and aid not in seen:
                seen.add(aid)
                artist_ids.append(aid)

    all_genres: list[str] = []
    for offset in range(0, len(artist_ids), 50):
        batch = artist_ids[offset : offset + 50]
        all_genres.extend(_batch_artist_genres(client, batch))

    return dominant_genre(all_genres)


def fetch_all_genres(
    client: spotipy.Spotify,
    playlist_ids: list[str],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, str]:
    """Fetch genres for all playlists and merge into cache."""
    cached = load_genres()
    for i, playlist_id in enumerate(playlist_ids):
        if on_progress:
            on_progress(i, len(playlist_ids), playlist_id)
        genre = fetch_playlist_genre(client, playlist_id)
        if genre:
            cached[playlist_id] = genre
    save_genres(cached)
    return cached
