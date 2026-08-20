"""Tests for the daily progress report (no network, no email)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from djsync import agent, beeper, cache, downloads, events, quota, report
from djsync.config import Destination


@pytest.fixture
def isolated_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(downloads, "LEDGER_PATH", tmp_path / "downloads.json")
    monkeypatch.setattr(quota, "LEDGER_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.json")
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(agent, "STATE_PATH", tmp_path / "agent.json")
    return tmp_path


def _dest(tmp_path: Path, *, mounted: bool = True) -> Destination:
    drive = tmp_path / "drive"
    if mounted:
        drive.mkdir(parents=True)
    return Destination(
        drive=drive,
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )


def _playlist_entry(downloaded: int, total: int) -> dict[str, Any]:
    return {
        "id": "pl1",
        "name": "House",
        "sigils": ["d"],
        "track_count": total,
        "downloaded_count": downloaded,
    }


def _album_entry(downloaded: int, total: int) -> dict[str, Any]:
    return {
        "id": "al1",
        "name": "Album",
        "artists": ["Artist"],
        "total_tracks": total,
        "downloaded_count": downloaded,
    }


def _cache(
    playlists: list[dict[str, Any]] | None = None,
    albums: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": "2026-08-19T00:00:00+00:00",
        "playlist_catalog": [],
        "playlists": playlists or [],
        "albums": albums or [],
        "album_tracks": {},
        "collections": {"playlists": {"status": "ok"}, "albums": {"status": "ok"}},
    }


def test_report_renders_progress_from_synthetic_cache(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache(
        [_playlist_entry(40, 100)],
        [_album_entry(5, 10)],
    )
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    downloads.record_download(now=now - timedelta(hours=1))

    text = report.build_report(data=data, dest=dest, now=now)

    assert "Playlists" in text
    assert "40" in text and "100" in text
    assert "Albums" in text
    assert "5" in text and "10" in text


def test_projected_completion_uses_observed_rate_not_cap(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(downloads, "DAILY_DOWNLOAD_CAP", 800)
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache([_playlist_entry(0, 100)])

    for day in range(7):
        downloads.record_download(now=now - timedelta(days=day, hours=1))

    projected = report.projected_completion_date(data, now=now)
    assert projected is not None
    days_left = (projected - now).total_seconds() / 86400
    # 7 downloads over ~7 days => ~1/day => ~100 days, not ~0.125 days at cap 800
    assert days_left > 50


def test_nothing_happened_short_form(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache([_playlist_entry(10, 100)])
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)

    text = report.build_report(data=data, dest=dest, now=now)

    assert text == "djsync: no activity in the last 24 hours."
    assert "Progress" not in text


def test_report_includes_failures_quota_and_explicit_review(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache([_playlist_entry(10, 100)])
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)

    downloads.record_download(now=now - timedelta(hours=2))
    events.record_failure("yt-dlp timeout", now=now - timedelta(hours=1))
    events.record_unverified_explicit(
        {
            "track_id": "t1",
            "name": "Bad Song",
            "artists": ["DJ"],
            "chosen_title": "Clean Upload",
        },
        now=now - timedelta(hours=1),
    )
    quota.record_request(now=now - timedelta(hours=3))

    text = report.build_report(data=data, dest=dest, now=now)

    assert "Tracks downloaded: 1" in text
    assert "yt-dlp timeout" in text
    assert "Bad Song" in text
    assert "Clean Upload" in text
    assert "Spotify quota" in text


def test_email_path_invokes_applescript_mock(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache([_playlist_entry(0, 10)])
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    downloads.record_download(now=now - timedelta(hours=1))

    calls: list[list[str]] = []

    def fake_osascript(args: list[str], **_kwargs: object) -> MagicMock:
        calls.append(args)
        return MagicMock(returncode=0)

    code = report.run_report(
        email=True,
        recipient="test@example.com",
        now=now,
        dest=dest,
        run_osascript=fake_osascript,
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == ""
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    script = calls[0][2]
    assert "Mail" in script
    assert "test@example.com" in script
    assert "djsync report" in script


def test_report_stdout_when_not_email(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    downloads.record_download(now=now - timedelta(hours=1))

    code = report.run_report(email=False, now=now, dest=dest)
    out = capsys.readouterr().out

    assert code == 0
    assert "djsync daily report" in out


def test_albums_zero_of_zero_shows_not_scanned(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache([_playlist_entry(10, 100)], albums=[])
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    downloads.record_download(now=now - timedelta(hours=1))

    text = report.build_report(data=data, dest=dest, now=now)

    assert "not scanned yet" in text
    assert "100.0%" not in text.split("Albums")[1].split("\n")[0]


def test_report_embed_color_green_by_default(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache([_playlist_entry(10, 100)])
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    downloads.record_download(now=now - timedelta(hours=1))

    color = report.report_embed_color(data=data, dest=dest, now=now)

    assert color == beeper.COLOR_GREEN


def test_report_embed_color_amber_on_failures(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache([_playlist_entry(10, 100)])
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    downloads.record_download(now=now - timedelta(hours=1))
    events.record_failure("timeout", now=now - timedelta(hours=1))

    color = report.report_embed_color(data=data, dest=dest, now=now)

    assert color == beeper.COLOR_AMBER


def test_report_embed_color_red_on_lockout(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    data = _cache([_playlist_entry(10, 100)])
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    downloads.record_download(now=now - timedelta(hours=1))
    quota.record_429("rate limit", retry_after_seconds=7200, now=now)

    color = report.report_embed_color(data=data, dest=dest, now=now)

    assert color == beeper.COLOR_RED


def test_wrap_report_body_code_block() -> None:
    body = "Progress\n  Playlists      1 / 2\n\nLast 24 hours"
    wrapped = beeper.wrap_report_body(body)
    assert wrapped.startswith("```")
    assert "Progress" in wrapped
    assert "Last 24 hours" in wrapped


def test_beeper_failed_post_records_state_without_raising(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setenv("DJSYNC_BEEPER_TOKEN", "test-token")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("network down")

    ok = beeper.send_message("hello", urlopen=boom)

    assert ok is False
    state = agent.load_state()
    assert state.get("last_beeper_error") == "network down"
    assert state.get("last_beeper_failure")


def test_missing_beeper_token_prints_helpful_message(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    monkeypatch.setenv("DJSYNC_BEEPER_TOKEN", "")
    downloads.record_download(now=now - timedelta(hours=1))

    code = report.run_report(beeper_post=True, now=now, dest=dest)
    out = capsys.readouterr().out

    assert code == 1
    assert "DJSYNC_BEEPER_TOKEN" in out
    assert "beeper-auth" in out


def test_beeper_report_posts_message(
    isolated_report: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    dest = _dest(isolated_report)
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)
    monkeypatch.setenv("DJSYNC_BEEPER_TOKEN", "test-token")
    downloads.record_download(now=now - timedelta(hours=1))

    posted: list[dict[str, Any]] = []

    def fake_urlopen(req: object, **_kwargs: object) -> MagicMock:
        url = getattr(req, "full_url", None) or getattr(req, "url", "")
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        if "/v1/info" in str(url):
            resp.read = MagicMock(return_value=b"{}")
            return resp
        body = getattr(req, "data", b"")
        posted.append(json.loads(body.decode()))
        return resp

    code = report.run_report(beeper_post=True, now=now, dest=dest, urlopen=fake_urlopen)
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == ""
    assert len(posted) == 1
    assert "Progress" in posted[0]["text"]
