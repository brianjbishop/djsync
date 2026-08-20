"""Tests for cache refresh, snapshot reuse, album caching, and rate limits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from spotipy.exceptions import SpotifyException

from djsync import cache, quota, spotify
from djsync.config import Destination


class FakeSpotifyClient:
    """In-memory Spotify client that records API calls."""

    def __init__(
        self,
        *,
        playlists: list[dict[str, Any]] | None = None,
        playlist_tracks: dict[str, list[dict[str, Any]]] | None = None,
        saved_albums: list[dict[str, Any]] | None = None,
        album_tracks: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._playlists = playlists or []
        self._playlist_tracks = playlist_tracks or {}
        self._saved_albums = saved_albums or []
        self._album_tracks = album_tracks or {}
        self.playlist_tracks_calls: list[str] = []
        self.album_tracks_calls: list[str] = []
        self.current_user_playlists_calls: list[tuple[int, int]] = []

    def current_user_playlists(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.current_user_playlists_calls.append((limit, offset))
        page = self._playlists[offset : offset + limit]
        has_more = offset + limit < len(self._playlists)
        return {
            "items": page,
            "next": "more" if has_more else None,
        }

    def playlist_tracks(
        self, playlist_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        self.playlist_tracks_calls.append(playlist_id)
        tracks = self._playlist_tracks.get(playlist_id, [])
        page = tracks[offset : offset + limit]
        has_more = offset + limit < len(tracks)
        return {
            "items": page,
            "next": "more" if has_more else None,
        }

    def current_user_saved_albums(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        page = self._saved_albums[offset : offset + limit]
        has_more = offset + limit < len(self._saved_albums)
        return {
            "items": page,
            "next": "more" if has_more else None,
        }

    def album_tracks(
        self, album_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        self.album_tracks_calls.append(album_id)
        tracks = self._album_tracks.get(album_id, [])
        page = tracks[offset : offset + limit]
        has_more = offset + limit < len(tracks)
        return {
            "items": page,
            "next": "more" if has_more else None,
        }


def _playlist_item(
    *,
    id_: str,
    name: str,
    snapshot_id: str,
    track_count: int = 1,
) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "snapshot_id": snapshot_id,
        "tracks": {"total": track_count},
    }


def _playlist_track_item(
    *,
    track_id: str,
    added_at: str = "2024-06-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "added_at": added_at,
        "track": {
            "id": track_id,
            "name": f"Track {track_id}",
            "artists": [{"id": "artist1", "name": "Artist"}],
            "album": {"name": "Album"},
            "duration_ms": 180_000,
            "explicit": False,
        },
    }


def _saved_album_item(*, album_id: str, name: str = "Album One") -> dict[str, Any]:
    return {
        "added_at": "2024-01-01T00:00:00Z",
        "album": {
            "id": album_id,
            "name": name,
            "artists": [{"id": "a1", "name": "Artist"}],
            "total_tracks": 1,
            "release_date": "2024",
            "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
            "uri": f"spotify:album:{album_id}",
        },
    }


def _album_track_item(*, track_id: str) -> dict[str, Any]:
    return {
        "id": track_id,
        "name": f"Album Track {track_id}",
        "artists": [{"id": "a1", "name": "Artist"}],
        "duration_ms": 200_000,
        "track_number": 1,
        "disc_number": 1,
        "explicit": False,
    }


@pytest.fixture(autouse=True)
def _isolated_quota_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quota, "LEDGER_PATH", tmp_path / "quota.json")


@pytest.fixture
def dest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Destination:
    destination = Destination(
        drive=tmp_path / "drive",
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    monkeypatch.setattr("djsync.cache.get_destination", lambda: destination)
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "cache.json")
    return destination


def test_unchanged_snapshot_skips_fetch_tracks(dest: Destination) -> None:
    playlist_id = "pl1"
    client = FakeSpotifyClient(
        playlists=[_playlist_item(id_=playlist_id, name="$d Test", snapshot_id="snap-v1")],
        playlist_tracks={
            playlist_id: [_playlist_track_item(track_id="t1", added_at="2024-05-01T00:00:00Z")],
        },
    )
    prior = {
        "playlists": [
            {
                "id": playlist_id,
                "name": "$d Test",
                "sigils": ["d"],
                "track_count": 1,
                "snapshot_id": "snap-v1",
                "tracks": [{"id": "t1", "duration_ms": 180_000, "added_at": "2024-05-01T00:00:00Z"}],
                "downloaded_count": 0,
                "status": "none",
                "last_added": "2024-05-01T00:00:00Z",
            }
        ],
        "albums": [],
        "album_tracks": {},
        "playlist_catalog": [],
    }

    data = cache.build_cache(client, prior=prior)

    assert client.playlist_tracks_calls == []
    entry = data["playlists"][0]
    assert entry["downloaded_count"] == 0
    assert entry["last_added"] == "2024-05-01T00:00:00Z"


def test_changed_snapshot_refetches_tracks(dest: Destination) -> None:
    playlist_id = "pl1"
    client = FakeSpotifyClient(
        playlists=[_playlist_item(id_=playlist_id, name="$d Test", snapshot_id="snap-v2")],
        playlist_tracks={
            playlist_id: [_playlist_track_item(track_id="t2", added_at="2024-07-01T00:00:00Z")],
        },
    )
    prior = {
        "playlists": [
            {
                "id": playlist_id,
                "name": "$d Test",
                "sigils": ["d"],
                "track_count": 1,
                "snapshot_id": "snap-v1",
                "tracks": [{"id": "t1", "duration_ms": 180_000, "added_at": "2024-05-01T00:00:00Z"}],
            }
        ],
        "albums": [],
        "album_tracks": {},
        "playlist_catalog": [],
    }

    data = cache.build_cache(client, prior=prior)

    assert client.playlist_tracks_calls == [playlist_id]
    entry = data["playlists"][0]
    assert entry["last_added"] == "2024-07-01T00:00:00Z"
    assert entry["tracks"][0]["id"] == "t2"


def test_album_tracks_fetched_once_and_reused(dest: Destination) -> None:
    album_id = "alb1"
    client = FakeSpotifyClient(
        playlists=[],
        saved_albums=[_saved_album_item(album_id=album_id)],
        album_tracks={album_id: [_album_track_item(track_id="at1")]},
    )

    first = cache.build_cache(client, prior={"playlists": [], "albums": [], "album_tracks": {}})
    assert client.album_tracks_calls == [album_id]

    client.album_tracks_calls.clear()
    second = cache.build_cache(client, prior=first)

    assert client.album_tracks_calls == []
    assert second["albums"][0]["downloaded_count"] == 0
    assert album_id in second["album_tracks"]


def test_rate_limit_error_parses_retry_after() -> None:
    headers = {"Retry-After": "77599"}
    exc = SpotifyException(429, -1, "rate limited", headers=headers)

    with pytest.raises(spotify.RateLimitedError) as err:
        spotify._spotify_call(_raise_spotify, exc)

    assert err.value.retry_after_seconds == 77599
    assert "77599 s" in str(err.value)
    assert "per-application" in str(err.value)


def _raise_spotify(exc: SpotifyException) -> None:
    raise exc


def test_skipped_playlist_recomputes_downloaded_count_from_local_files(
    dest: Destination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist_id = "pl1"
    playlist_name = "$d Local"
    monkeypatch.setattr(
        "djsync.cache.existing_spotify_ids",
        lambda _folder: {"t1"},
    )

    client = FakeSpotifyClient(
        playlists=[_playlist_item(id_=playlist_id, name=playlist_name, snapshot_id="snap-v1")],
    )
    prior = {
        "playlists": [
            {
                "id": playlist_id,
                "name": playlist_name,
                "sigils": ["d"],
                "track_count": 1,
                "snapshot_id": "snap-v1",
                "tracks": [{"id": "t1", "duration_ms": 180_000, "added_at": "2024-08-01T00:00:00Z"}],
            }
        ],
        "albums": [],
        "album_tracks": {},
        "playlist_catalog": [],
    }

    data = cache.build_cache(client, prior=prior)

    assert client.playlist_tracks_calls == []
    entry = data["playlists"][0]
    assert entry["downloaded_count"] == 1
    assert entry["status"] == "complete"
    assert entry["last_added"] == "2024-08-01T00:00:00Z"


def test_max_playlists_limits_track_refetch(dest: Destination) -> None:
    playlists = [
        _playlist_item(id_="pl1", name="$d One", snapshot_id="snap-new-1"),
        _playlist_item(id_="pl2", name="$d Two", snapshot_id="snap-new-2"),
    ]
    client = FakeSpotifyClient(
        playlists=playlists,
        playlist_tracks={
            "pl1": [_playlist_track_item(track_id="t1")],
            "pl2": [_playlist_track_item(track_id="t2")],
        },
    )
    prior = {
        "playlists": [
            {
                "id": "pl1",
                "name": "$d One",
                "sigils": ["d"],
                "track_count": 1,
                "snapshot_id": "snap-old-1",
                "tracks": [{"id": "old1", "duration_ms": 1, "added_at": "2024-01-01T00:00:00Z"}],
            },
            {
                "id": "pl2",
                "name": "$d Two",
                "sigils": ["d"],
                "track_count": 1,
                "snapshot_id": "snap-old-2",
                "tracks": [{"id": "old2", "duration_ms": 1, "added_at": "2024-01-01T00:00:00Z"}],
            },
        ],
        "albums": [],
        "album_tracks": {},
        "playlist_catalog": [],
    }

    cache.build_cache(client, prior=prior, max_playlists=1)

    assert len(client.playlist_tracks_calls) == 1


def test_catalog_playlists_for_ui_marks_unscanned() -> None:
    data = {
        "playlist_catalog": [
            {"id": "p1", "name": "$d One", "track_count": 1, "sigils": ["d"], "snapshot_id": "s1"},
            {"id": "p2", "name": "Plain", "track_count": 3, "sigils": [], "snapshot_id": "s2"},
        ],
        "playlists": [
            {
                "id": "p1",
                "name": "$d One",
                "track_count": 1,
                "sigils": ["d"],
                "snapshot_id": "s1",
                "tracks": [{"id": "t1", "duration_ms": 1, "added_at": "2024-01-01T00:00:00Z"}],
                "downloaded_count": 0,
                "status": "none",
            }
        ],
    }
    rows = cache.catalog_playlists_for_ui(data)
    assert len(rows) == 2
    by_id = {row["id"]: row for row in rows}
    assert by_id["p1"]["status"] == "none"
    assert by_id["p1"]["scanned"] is True
    assert by_id["p2"]["status"] == "not_scanned"
    assert by_id["p2"]["downloaded_count"] is None


def test_playlists_from_catalog_rebuilds_playlist_objects() -> None:
    data = {
        "playlist_catalog": [
            {
                "id": "p1",
                "name": "$d Demo",
                "track_count": 3,
                "sigils": ["d"],
                "snapshot_id": "abc",
            }
        ]
    }
    playlists = cache.playlists_from_catalog(data)
    assert len(playlists) == 1
    assert playlists[0].snapshot_id == "abc"
    assert playlists[0].sigils == frozenset({"d"})


def test_spotify_exception_non_429_is_reraised() -> None:
    exc = SpotifyException(500, -1, "server error", headers={})
    with pytest.raises(SpotifyException):
        spotify._spotify_call(_raise_spotify, exc)


def test_rate_limit_without_header_defaults_to_zero() -> None:
    exc = SpotifyException(429, -1, "rate limited", headers={})
    with pytest.raises(spotify.RateLimitedError) as err:
        spotify._spotify_call(_raise_spotify, exc)
    assert err.value.retry_after_seconds == 0
