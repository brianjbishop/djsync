"""Catalog TTL, force-catalog, estimate gating, and budget-aware batching."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

from djsync import agent, cache, config, downloads, events, quota
from djsync.config import Destination
from djsync.sync import SyncResult

from tests.test_cache import (
    FakeSpotifyClient,
    _playlist_item,
    _playlist_track_item,
)


@contextmanager
def _null_prevent_sleep() -> Iterator[None]:
    yield None


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
    monkeypatch.setattr(quota, "LEDGER_PATH", tmp_path / "quota.json")
    return destination


def _catalog_entry(
    *,
    playlist_id: str,
    name: str,
    snapshot_id: str = "snap-v1",
    track_count: int = 1,
) -> dict[str, Any]:
    return {
        "id": playlist_id,
        "name": name,
        "track_count": track_count,
        "sigils": ["d"],
        "snapshot_id": snapshot_id,
    }


def _scanned_entry(
    *,
    playlist_id: str,
    name: str,
    snapshot_id: str = "snap-v1",
    track_id: str = "t1",
) -> dict[str, Any]:
    return {
        "id": playlist_id,
        "name": name,
        "sigils": ["d"],
        "track_count": 1,
        "snapshot_id": snapshot_id,
        "tracks": [
            {
                "id": track_id,
                "name": "Track",
                "artists": ["Artist"],
                "album": "Album",
                "duration_ms": 180_000,
                "isrc": None,
                "added_at": "2024-06-01T00:00:00Z",
                "artist_ids": [],
                "explicit": False,
            }
        ],
        "downloaded_count": 0,
        "status": "none",
        "last_added": "2024-06-01T00:00:00Z",
    }


def test_fresh_catalog_skips_fetch_playlists(dest: Destination) -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    client = FakeSpotifyClient(
        playlists=[_playlist_item(id_="pl1", name="$d One", snapshot_id="snap-v1")],
        playlist_tracks={"pl1": [_playlist_track_item(track_id="t1")]},
    )
    # Catalog already knows snapshot changed; track data is stale. Reuse catalog
    # metadata and spend the budget on fetch_tracks only.
    prior = {
        "timestamp": now.isoformat(),
        "catalog_fetched_at": now.isoformat(),
        "playlist_catalog": [
            _catalog_entry(playlist_id="pl1", name="$d One", snapshot_id="snap-new"),
        ],
        "playlists": [
            _scanned_entry(
                playlist_id="pl1", name="$d One", snapshot_id="snap-old", track_id="old"
            )
        ],
        "albums": [],
        "album_tracks": {},
        "collections": {
            "playlists": {"status": "ok"},
            "albums": {"status": "never_fetched"},
        },
    }
    fetch_calls: list[object] = []

    def boom(*_args: object, **_kwargs: object) -> list[object]:
        fetch_calls.append(1)
        raise AssertionError("fetch_playlists must not be called for a fresh catalog")

    import djsync.spotify as spotify_mod

    original = spotify_mod.fetch_playlists
    spotify_mod.fetch_playlists = boom  # type: ignore[assignment]
    try:
        cache.build_cache(
            client,
            prior=prior,
            max_playlists=1,
            sync_albums=False,
            now=now,
        )
    finally:
        spotify_mod.fetch_playlists = original  # type: ignore[assignment]

    assert fetch_calls == []
    assert client.current_user_playlists_calls == []
    assert client.playlist_tracks_calls == ["pl1"]


def test_stale_catalog_triggers_one_relist(
    dest: Destination,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "CATALOG_TTL_HOURS", 24)
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    stale_at = (now - timedelta(hours=25)).isoformat()
    client = FakeSpotifyClient(
        playlists=[_playlist_item(id_="pl1", name="$d One", snapshot_id="snap-v2")],
        playlist_tracks={"pl1": [_playlist_track_item(track_id="t2")]},
    )
    prior = {
        "timestamp": stale_at,
        "catalog_fetched_at": stale_at,
        "playlist_catalog": [
            _catalog_entry(playlist_id="pl1", name="$d One", snapshot_id="snap-v1"),
        ],
        "playlists": [
            _scanned_entry(playlist_id="pl1", name="$d One", snapshot_id="snap-v1")
        ],
        "albums": [],
        "album_tracks": {},
    }
    cache.build_cache(
        client,
        prior=prior,
        max_playlists=1,
        sync_albums=False,
        now=now,
    )
    assert len(client.current_user_playlists_calls) == 1
    assert client.playlist_tracks_calls == ["pl1"]


def test_force_catalog_relists_when_fresh(dest: Destination) -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    client = FakeSpotifyClient(
        playlists=[_playlist_item(id_="pl1", name="$d One", snapshot_id="snap-v1")],
        playlist_tracks={"pl1": [_playlist_track_item(track_id="t1")]},
    )
    prior = {
        "timestamp": now.isoformat(),
        "catalog_fetched_at": now.isoformat(),
        "playlist_catalog": [
            _catalog_entry(playlist_id="pl1", name="$d One", snapshot_id="snap-v1"),
        ],
        "playlists": [
            _scanned_entry(playlist_id="pl1", name="$d One", snapshot_id="snap-v1")
        ],
        "albums": [],
        "album_tracks": {},
    }
    cache.build_cache(
        client,
        prior=prior,
        max_playlists=1,
        sync_albums=False,
        force_catalog=True,
        now=now,
    )
    assert len(client.current_user_playlists_calls) == 1
    assert client.playlist_tracks_calls == []


def test_estimate_includes_listing_only_when_catalog_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("djsync.config.SYNC_ALBUMS", False)
    monkeypatch.setattr("djsync.config.CATALOG_TTL_HOURS", 24)
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    catalog = [
        {
            "id": f"p{i}",
            "name": f"$d P{i}" if i < 2 else f"P{i}",
            "sigils": ["d"] if i < 2 else [],
            "track_count": 50,
            "snapshot_id": "s",
        }
        for i in range(1051)
    ]
    needing = [
        {
            "id": "p0",
            "name": "$d P0",
            "sigils": ["d"],
            "track_count": 50,
            "snapshot_id": "old",
            "tracks": [{"id": "old0", "duration_ms": 1}],
        },
        {
            "id": "p1",
            "name": "$d P1",
            "sigils": ["d"],
            "track_count": 50,
            "snapshot_id": "old",
            "tracks": [{"id": "old1", "duration_ms": 1}],
        },
    ]
    fresh_prior = {
        "catalog_fetched_at": now.isoformat(),
        "playlist_catalog": catalog,
        "playlists": needing,
        "albums": [],
        "album_tracks": {},
    }
    stale_prior = {
        "catalog_fetched_at": (now - timedelta(hours=30)).isoformat(),
        "playlist_catalog": catalog,
        "playlists": needing,
        "albums": [],
        "album_tracks": {},
    }
    fresh_cost = quota.estimate_refresh_cost(fresh_prior, now=now)
    stale_cost = quota.estimate_refresh_cost(stale_prior, now=now)
    assert fresh_cost == 2  # two $d playlists needing refetch, 1 page each
    assert stale_cost == 22 + 2  # ceil(1051/50) listing + track pages
    assert stale_cost > fresh_cost


def test_max_playlists_fitting_budget_shrinks_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("djsync.config.SYNC_ALBUMS", False)
    monkeypatch.setattr("djsync.config.CATALOG_TTL_HOURS", 24)
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    catalog = [
        {
            "id": f"p{i}",
            "name": f"$d P{i}",
            "sigils": ["d"],
            "track_count": 1,
            "snapshot_id": "new",
        }
        for i in range(10)
    ]
    prior = {
        "catalog_fetched_at": now.isoformat(),
        "playlist_catalog": catalog,
        "playlists": [],
        "albums": [],
        "album_tracks": {},
    }
    # Fresh catalog: each playlist costs 1 track page. Remaining 7 → at most 7.
    assert (
        quota.max_playlists_fitting_budget(prior, remaining=7, desired=10, now=now) == 7
    )
    assert (
        quota.max_playlists_fitting_budget(prior, remaining=41, desired=10, now=now)
        == 10
    )


@pytest.fixture
def isolated_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(quota, "LEDGER_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(downloads, "LEDGER_PATH", tmp_path / "downloads.json")
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.json")
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(agent, "STATE_PATH", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOCK_PATH", tmp_path / "agent.lock")
    monkeypatch.setattr(downloads, "DAILY_DOWNLOAD_CAP", 800)
    monkeypatch.setattr("djsync.spotify.get_client", lambda: object())
    monkeypatch.setattr(agent, "sync_album", MagicMock(return_value=SyncResult()))
    monkeypatch.setattr(agent, "_sleep_between_downloads", lambda: None)
    monkeypatch.setattr(agent, "prevent_sleep", _null_prevent_sleep)
    return tmp_path


def _agent_dest(tmp_path: Path) -> Destination:
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    (drive / "dj" / "playlists").mkdir(parents=True, exist_ok=True)
    (drive / "dj" / "albums").mkdir(parents=True, exist_ok=True)
    return Destination(
        drive=drive,
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )


def _mount(monkeypatch: pytest.MonkeyPatch, dest: Destination) -> None:
    drive = dest.drive

    def ismount(path: str | os.PathLike[str]) -> bool:
        return Path(path) == drive

    monkeypatch.setattr(os.path, "ismount", ismount)


def test_agent_shrinks_refresh_batch_to_remaining_budget(
    isolated_agent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = _agent_dest(isolated_agent)
    _mount(monkeypatch, dest)
    monkeypatch.setattr(agent, "PLAYLISTS_PER_RUN", 10)
    monkeypatch.setattr(quota, "DAILY_REQUEST_BUDGET", 300)
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    old = (now - timedelta(hours=20)).isoformat()
    catalog = [
        {
            "id": f"p{i}",
            "name": f"$d P{i}",
            "sigils": ["d"],
            "track_count": 1,
            "snapshot_id": "s",
        }
        for i in range(10)
    ]
    cache.save_cache(
        {
            "timestamp": old,
            "catalog_fetched_at": now.isoformat(),
            "playlist_catalog": catalog,
            "playlists": [
                {
                    "id": "p0",
                    "name": "$d P0",
                    "sigils": ["d"],
                    "track_count": 2,
                    "snapshot_id": "s",
                    "tracks": [
                        {
                            "id": "t0",
                            "name": "A",
                            "artists": ["X"],
                            "album": "Y",
                            "duration_ms": 1,
                            "isrc": None,
                            "added_at": "2024-01-01T00:00:00Z",
                            "artist_ids": [],
                            "explicit": False,
                        },
                        {
                            "id": "t1",
                            "name": "B",
                            "artists": ["X"],
                            "album": "Y",
                            "duration_ms": 1,
                            "isrc": None,
                            "added_at": "2024-01-01T00:00:00Z",
                            "artist_ids": [],
                            "explicit": False,
                        },
                    ],
                    "downloaded_count": 0,
                    "status": "none",
                    "last_added": "2024-01-01T00:00:00Z",
                }
            ],
            "albums": [],
            "album_tracks": {},
            "collections": {
                "playlists": {"status": "ok"},
                "albums": {"status": "never_fetched"},
            },
        }
    )
    # 293 used → 7 remaining. Fresh catalog, each never-fetched playlist = 1 req.
    for _ in range(293):
        quota.record_request(now=now)

    seen: dict[str, int | None] = {"max_playlists": None}

    def fake_refresh(*, max_playlists: int | None = None) -> dict[str, Any]:
        seen["max_playlists"] = max_playlists
        return cache.load_cache() or {}

    monkeypatch.setattr(agent, "refresh_cache", fake_refresh)
    monkeypatch.setattr(
        agent, "sync_playlist", lambda *_a, **_k: SyncResult(downloaded=1)
    )
    monkeypatch.setattr(agent, "apply_local_progress", lambda *a, **k: None)

    code = agent.run_agent(dest=dest, notify=lambda _m: None, now=now)

    assert code == 0
    assert seen["max_playlists"] == 7


def test_agent_refreshes_nothing_when_budget_nearly_exhausted_still_downloads(
    isolated_agent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = _agent_dest(isolated_agent)
    _mount(monkeypatch, dest)
    monkeypatch.setattr(agent, "PLAYLISTS_PER_RUN", 10)
    monkeypatch.setattr(quota, "DAILY_REQUEST_BUDGET", 300)
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    old = (now - timedelta(hours=20)).isoformat()
    cache.save_cache(
        {
            "timestamp": old,
            "catalog_fetched_at": (now - timedelta(hours=30)).isoformat(),
            "playlist_catalog": [
                {
                    "id": f"p{i}",
                    "name": f"P{i}",
                    "sigils": ["d"] if i == 0 else [],
                    "track_count": 1,
                    "snapshot_id": "s",
                }
                for i in range(1051)
            ],
            "playlists": [
                {
                    "id": "p0",
                    "name": "$d Work",
                    "sigils": ["d"],
                    "track_count": 2,
                    "snapshot_id": "s",
                    "tracks": [
                        {
                            "id": "t0",
                            "name": "A",
                            "artists": ["X"],
                            "album": "Y",
                            "duration_ms": 1,
                            "isrc": None,
                            "added_at": "2024-01-01T00:00:00Z",
                            "artist_ids": [],
                            "explicit": False,
                        },
                        {
                            "id": "t1",
                            "name": "B",
                            "artists": ["X"],
                            "album": "Y",
                            "duration_ms": 1,
                            "isrc": None,
                            "added_at": "2024-01-01T00:00:00Z",
                            "artist_ids": [],
                            "explicit": False,
                        },
                    ],
                    "downloaded_count": 0,
                    "status": "none",
                    "last_added": "2024-01-01T00:00:00Z",
                }
            ],
            "albums": [],
            "album_tracks": {},
            "collections": {
                "playlists": {"status": "ok"},
                "albums": {"status": "never_fetched"},
            },
        }
    )
    # Listing alone costs 22; leave only 5 requests → cannot refresh.
    for _ in range(295):
        quota.record_request(now=now)

    refresh = MagicMock()
    monkeypatch.setattr(agent, "refresh_cache", refresh)
    monkeypatch.setattr(
        agent, "sync_playlist", lambda *_a, **_k: SyncResult(downloaded=1)
    )
    monkeypatch.setattr(agent, "apply_local_progress", lambda *a, **k: None)

    code = agent.run_agent(dest=dest, notify=lambda _m: None, now=now)

    assert code == 0
    refresh.assert_not_called()
    assert downloads.used_last_24h(now=now) == 2
