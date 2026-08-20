"""Tests for cache-first sync and collection fetch status."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from djsync import cache, spotify, sync
from djsync.config import Destination
from djsync.models import Track
from tests.test_cache import (
    FakeSpotifyClient,
    _playlist_item,
    _playlist_track_item,
)


@pytest.fixture
def dest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Destination:
    destination = Destination(
        drive=tmp_path / "drive",
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    monkeypatch.setattr("djsync.cache.get_destination", lambda: destination)
    monkeypatch.setattr("djsync.sync.get_destination", lambda: destination)
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "cache.json")
    return destination


def _warm_playlist_cache(*, playlist_id: str = "pl1", name: str = "$d Warm") -> dict[str, Any]:
    track = Track(
        id="t1",
        name="Cached Track",
        artists=("Artist",),
        album="Album",
        duration_ms=180_000,
        isrc=None,
        added_at="2024-06-01T00:00:00Z",
    )
    entry = {
        "id": playlist_id,
        "name": name,
        "sigils": ["d"],
        "track_count": 1,
        "snapshot_id": "snap-v1",
        "tracks": cache._serialize_tracks([track]),
        "downloaded_count": 1,
        "status": "complete",
        "last_added": "2024-06-01T00:00:00Z",
        "spotify_url": f"https://open.spotify.com/playlist/{playlist_id}",
        "spotify_uri": f"spotify:playlist:{playlist_id}",
    }
    return {
        "timestamp": "2024-08-01T00:00:00Z",
        "playlist_catalog": [],
        "playlists": [entry],
        "albums": [],
        "album_tracks": {},
        "collections": {
            "playlists": {"status": "ok"},
            "albums": {"status": "never_fetched"},
        },
    }


def test_sync_from_warm_cache_makes_zero_spotify_calls(
    dest: Destination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _warm_playlist_cache()
    client = FakeSpotifyClient()
    playlist = cache.playlist_from_entry(data["playlists"][0])
    tracks = cache.cached_playlist_tracks(data["playlists"][0])
    assert tracks is not None

    monkeypatch.setattr(
        "djsync.sync.search.search_candidates",
        lambda *_args, **_kwargs: [],
    )

    result = sync.sync_playlist(
        client,
        playlist,
        tracks=tracks,
        dry_run=True,
        cached=data,
    )

    assert client.playlist_tracks_calls == []
    assert client.current_user_playlists_calls == []
    assert result.failed + result.skipped >= 0


def test_sync_with_refresh_calls_spotify(
    dest: Destination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist_id = "pl1"
    name = "$d Warm"
    data = _warm_playlist_cache(playlist_id=playlist_id, name=name)
    client = FakeSpotifyClient(
        playlists=[_playlist_item(id_=playlist_id, name=name, snapshot_id="snap-v2")],
        playlist_tracks={
            playlist_id: [_playlist_track_item(track_id="t2")],
        },
    )

    monkeypatch.setattr(
        "djsync.sync.search.search_candidates",
        lambda *_args, **_kwargs: [],
    )

    playlist, tracks = cache.resolve_playlist_for_sync(
        client,
        name,
        refresh=True,
        cached=data,
    )

    assert client.playlist_tracks_calls == [playlist_id]
    assert len(client.current_user_playlists_calls) >= 1
    assert tracks[0].id == "t2"
    assert playlist.snapshot_id == "snap-v2"


def test_missing_cache_raises_refresh_first(dest: Destination) -> None:
    client = FakeSpotifyClient()
    with pytest.raises(cache.CacheDataError, match="refresh"):
        cache.resolve_playlist_for_sync(client, "$d Warm", refresh=False, cached=None)


def test_rate_limit_during_refresh_preserves_prior_cache(
    dest: Destination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = _warm_playlist_cache()
    cache.save_cache(prior)

    def _rate_limited(*_args, **_kwargs):
        raise spotify.RateLimitedError(3600)

    monkeypatch.setattr("djsync.cache.spotify.fetch_playlists", _rate_limited)
    client = FakeSpotifyClient()

    with pytest.raises(spotify.RateLimitedError):
        cache.build_cache(client, prior=prior)

    saved = cache.load_cache()
    assert saved is not None
    assert saved["playlists"][0]["name"] == "$d Warm"
    assert saved["collections"]["playlists"]["status"] == "rate_limited"
    assert saved["collections"]["playlists"]["retry_after_seconds"] == 3600
    assert saved["collections"]["playlists"]["reset_at"]


def test_collection_api_fields_distinguish_states() -> None:
    never = cache.collection_api_fields(None, "albums")
    assert never["status"] == "never_fetched"

    ok_empty = cache.collection_api_fields(
        {"albums": [], "collections": {"albums": {"status": "ok"}}},
        "albums",
    )
    assert ok_empty["status"] == "ok"

    limited = cache.collection_api_fields(
        {
            "albums": [{"id": "a1"}],
            "collections": {
                "albums": {
                    "status": "rate_limited",
                    "retry_after_seconds": 120,
                    "reset_at": "2024-08-01T01:00:00+00:00",
                }
            },
        },
        "albums",
    )
    assert limited["status"] == "rate_limited"
    assert limited["retry_after_seconds"] == 120
    assert limited["reset_at"] == "2024-08-01T01:00:00+00:00"


def test_album_sync_from_warm_cache_makes_zero_spotify_calls(
    dest: Destination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    album_id = "alb1"
    track = Track(
        id="at1",
        name="Album Track",
        artists=("Artist",),
        album="Album One",
        duration_ms=200_000,
        isrc=None,
        track_number=1,
        disc_number=1,
    )
    data = {
        "timestamp": "2024-08-01T00:00:00Z",
        "playlists": [],
        "albums": [
            {
                "id": album_id,
                "name": "Album One",
                "artists": ["Artist"],
                "total_tracks": 1,
                "downloaded_count": 1,
                "status": "complete",
                "added_at": "2024-01-01T00:00:00Z",
                "release_date": "2024",
                "spotify_url": f"https://open.spotify.com/album/{album_id}",
                "spotify_uri": f"spotify:album:{album_id}",
            }
        ],
        "album_tracks": {album_id: cache._serialize_album_tracks([track])},
        "playlist_catalog": [],
        "collections": {"playlists": {"status": "never_fetched"}, "albums": {"status": "ok"}},
    }
    client = FakeSpotifyClient()
    album = cache.album_from_entry(data["albums"][0])
    tracks = cache.cached_album_tracks(data, album_id, album_name=album.name)
    assert tracks is not None

    monkeypatch.setattr(
        "djsync.sync.search.search_candidates",
        lambda *_args, **_kwargs: [],
    )

    result = sync.sync_album(
        client,
        album,
        tracks=tracks,
        dry_run=True,
        cached=data,
    )

    assert client.album_tracks_calls == []
    assert result.failed + result.skipped >= 0
