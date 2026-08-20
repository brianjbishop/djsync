"""Tests for web API destination endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from djsync.config import Destination
from djsync import cache, spotify
from djsync.web import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DJSYNC_DRIVE", str(tmp_path / "drive"))
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "cache.json")
    dest = Destination(
        drive=tmp_path / "drive",
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    with patch("djsync.web.get_destination", return_value=dest):
        app = create_app()
        app.config["TESTING"] = True
        with app.test_client() as test_client:
            yield test_client, dest


def test_api_destination(client) -> None:
    test_client, dest = client
    resp = test_client.get("/api/destination")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["path"] == str(dest.path)
    assert data["mounted"] is False
    assert data["free_bytes"] is None
    assert data["free_human"] is None


def test_index_no_store_cache_control(client) -> None:
    test_client, _dest = client
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"


def test_api_refresh_rate_limited(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _dest = client
    exc = spotify.RateLimitedError(3600)

    def _boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr("djsync.web.spotify.get_client", lambda: object())
    monkeypatch.setattr("djsync.web.cache.build_cache", _boom)

    resp = test_client.post("/api/refresh", json={})
    assert resp.status_code == 429
    data = resp.get_json()
    assert data["rate_limited"] is True
    assert data["retry_after_seconds"] == 3600
    assert "per-application" in data["error"]


def test_api_sync_rejects_unmounted_drive(client) -> None:
    test_client, _dest = client
    resp = test_client.post(
        "/api/sync",
        json={"playlist_ids": ["abc"]},
    )
    assert resp.status_code == 409
    assert "not connected" in resp.get_json()["error"].lower()


def test_api_playlists_never_fetched_without_cache(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _dest = client
    monkeypatch.setattr("djsync.web.spotify.get_client", lambda: object())
    resp = test_client.get("/api/playlists")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "never_fetched"
    assert data["playlists"] == []


def test_api_albums_rate_limited_state_from_cache(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _dest = client
    monkeypatch.setattr("djsync.web.spotify.get_client", lambda: object())
    cache.save_cache(
        {
            "timestamp": "2024-08-01T00:00:00Z",
            "playlists": [],
            "albums": [{"id": "a1", "name": "Saved", "artists": ["A"], "total_tracks": 1, "downloaded_count": 0, "status": "none"}],
            "album_tracks": {},
            "playlist_catalog": [],
            "collections": {
                "playlists": {"status": "ok"},
                "albums": {
                    "status": "rate_limited",
                    "retry_after_seconds": 7200,
                    "reset_at": "2024-08-01T02:00:00+00:00",
                },
            },
        }
    )
    resp = test_client.get("/api/albums")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "rate_limited"
    assert data["retry_after_seconds"] == 7200
    assert data["reset_at"] == "2024-08-01T02:00:00+00:00"
    assert len(data["albums"]) == 1


def test_api_refresh_rate_limited_preserves_cached_rows(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _dest = client
    cache.save_cache(
        {
            "timestamp": "2024-08-01T00:00:00Z",
            "playlists": [
                {
                    "id": "p1",
                    "name": "$d Keep",
                    "sigils": ["d"],
                    "track_count": 1,
                    "snapshot_id": "s1",
                    "tracks": [{"id": "t1", "duration_ms": 1, "added_at": "2024-01-01T00:00:00Z"}],
                    "downloaded_count": 0,
                    "status": "none",
                }
            ],
            "albums": [],
            "album_tracks": {},
            "playlist_catalog": [],
            "collections": {"playlists": {"status": "ok"}, "albums": {"status": "never_fetched"}},
        }
    )

    def _boom(*_args, **_kwargs):
        prior = cache.load_cache()
        cache.record_collection_rate_limit(prior, "playlists", spotify.RateLimitedError(1800))
        cache.save_cache(prior)
        raise spotify.RateLimitedError(1800)

    monkeypatch.setattr("djsync.web.spotify.get_client", lambda: object())
    monkeypatch.setattr("djsync.web.cache.build_cache", _boom)

    resp = test_client.post("/api/refresh", json={})
    assert resp.status_code == 429
    data = resp.get_json()
    assert data["rate_limited"] is True
    assert len(data["playlists"]) == 1
    saved = cache.load_cache()
    assert saved["playlists"][0]["name"] == "$d Keep"
    assert saved["collections"]["playlists"]["status"] == "rate_limited"
