"""Tests for the Spotify request quota ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from spotipy.exceptions import SpotifyException

from djsync import quota, spotify


@pytest.fixture
def ledger_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "quota.json"
    monkeypatch.setattr(quota, "LEDGER_PATH", path)
    return path


def test_ledger_counts_requests_in_windows(ledger_path: Path) -> None:
    now = datetime(2024, 8, 1, 12, 0, 0, tzinfo=UTC)
    quota.record_request(now=now - timedelta(hours=25))
    quota.record_request(now=now - timedelta(seconds=20))
    quota.record_request(now=now - timedelta(seconds=5))

    assert quota.used_last_24h(now=now) == 2
    assert quota.used_last_30s(now=now) == 2


def test_can_spend_refuses_when_over_daily_budget(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quota, "DAILY_REQUEST_BUDGET", 2)
    now = datetime(2024, 8, 1, 12, 0, 0, tzinfo=UTC)
    quota.record_request(now=now)
    quota.record_request(now=now)

    assert quota.can_spend(1, now=now) is False
    assert quota.can_spend(0, now=now) is True


def test_recorded_429_blocks_calls_without_network(ledger_path: Path) -> None:
    # The lockout must be live relative to real "now" - _spotify_call checks the
    # wall clock, so a fixed past date would have expired and blocked nothing.
    quota.record_429("QUOTA_EXCEEDED", 3600, now=datetime.now(UTC))

    fn = MagicMock(return_value={"ok": True})
    with pytest.raises(spotify.RateLimitedError):
        spotify._spotify_call(fn)

    fn.assert_not_called()


def test_expired_429_no_longer_blocks(ledger_path: Path) -> None:
    """A lockout whose reset time has passed must not block forever."""
    stale = datetime.now(UTC) - timedelta(hours=5)
    quota.record_429("QUOTA_EXCEEDED", 3600, now=stale)

    fn = MagicMock(return_value={"ok": True})
    spotify._spotify_call(fn)
    fn.assert_called_once()


def test_successful_call_records_request(ledger_path: Path) -> None:
    fn = MagicMock(return_value={"items": []})
    spotify._spotify_call(fn)
    assert quota.used_last_24h() == 1


def test_spotify_429_records_lockout(ledger_path: Path) -> None:
    exc = SpotifyException(
        429,
        -1,
        "QUOTA_EXCEEDED",
        headers={"Retry-After": "120"},
    )

    def _raise(_exc: SpotifyException) -> None:
        raise _exc

    with pytest.raises(spotify.RateLimitedError) as err:
        spotify._spotify_call(_raise, exc)

    assert err.value.retry_after_seconds == 120
    assert quota.get_lockout() is not None
    assert quota.get_lockout()["reason"] == "QUOTA_EXCEEDED"


def test_estimate_refresh_cost_counts_catalog_and_refetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("djsync.config.SYNC_ALBUMS", True)
    prior = {
        "playlist_catalog": [{"id": f"p{i}", "name": f"P{i}", "sigils": ["d"] if i == 0 else [], "track_count": 1, "snapshot_id": "s"} for i in range(100)],
        "playlists": [],
        "albums": [{"id": "a1", "total_tracks": 1}],
        "album_tracks": {},
    }
    cost = quota.estimate_refresh_cost(prior)
    assert cost >= 2  # playlist pages + album page + refetch for one $d playlist


def test_estimate_refresh_cost_523_track_playlist_costs_six_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 523-track playlist costs 6 track requests (ceil(523/100)), not 1."""
    monkeypatch.setattr("djsync.config.SYNC_ALBUMS", False)
    assert quota._ceil_div(523, 100) == 6
    prior = {
        "playlist_catalog": [
            {
                "id": "big",
                "name": "$d Big",
                "sigils": ["d"],
                "track_count": 523,
                "snapshot_id": "s1",
            }
        ],
        "playlists": [],
        "albums": [],
        "album_tracks": {},
    }
    cost = quota.estimate_refresh_cost(prior)
    # 1 catalog listing page + 6 track pages (albums off).
    assert cost == 1 + 6
    assert cost != 1 + 1  # old bug: counted one request per playlist


def test_warning_shaped_429_is_recorded_in_ledger(ledger_path: Path) -> None:
    import logging

    def fn() -> dict[str, bool]:
        logging.getLogger("urllib3.util.retry").warning(
            "Retrying after ResponseError('too many 429 error responses') "
            "Retry-After: 5400"
        )
        return {"ok": True}

    with pytest.raises(spotify.RateLimitedError) as err:
        spotify._spotify_call(fn)

    assert err.value.retry_after_seconds == 5400
    lockout = quota.get_lockout()
    assert lockout is not None
    assert lockout["retry_after_seconds"] == 5400


def test_min_request_interval_enforced_between_calls(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"t": 100.0}
    slept: list[float] = []

    monkeypatch.setattr(spotify, "SPOTIFY_MIN_REQUEST_INTERVAL", 1.5)
    monkeypatch.setattr(spotify, "_last_request_at", None)
    monkeypatch.setattr(spotify, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(spotify, "_sleep", lambda seconds: slept.append(seconds))

    spotify._spotify_call(lambda: {"a": 1})
    clock["t"] = 100.4  # only 0.4s later
    spotify._spotify_call(lambda: {"b": 2})

    assert slept == [pytest.approx(1.1)]
