"""Tests for Beeper Desktop delivery and two-way commands (no network)."""

from __future__ import annotations

import errno
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from djsync import agent, beeper, cache, downloads
from djsync.config import Destination
from djsync.sync import SyncResult


@pytest.fixture
def isolated_beeper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(agent, "STATE_PATH", tmp_path / "agent.json")
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(downloads, "LEDGER_PATH", tmp_path / "downloads.json")
    monkeypatch.setenv("DJSYNC_BEEPER_URL", "http://127.0.0.1:23373")
    monkeypatch.setenv("DJSYNC_BEEPER_CHAT_ID", "33169")
    monkeypatch.setenv("DJSYNC_BEEPER_TOKEN", "test-token")
    return tmp_path


def _fake_beeper_urlopen(
    posts: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    *,
    info_ok: bool = True,
) -> MagicMock:
    posts = posts if posts is not None else []
    messages = messages if messages is not None else []

    def fake_urlopen(req: object, **_kwargs: object) -> MagicMock:
        url = getattr(req, "full_url", None) or getattr(req, "url", "")
        method = getattr(req, "method", "GET")
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)

        # When Beeper Desktop is down, nothing on the port answers - not just
        # /v1/info. Modelling only the info probe as failing let a test pass
        # against behaviour that cannot occur in reality.
        if not info_ok:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")

        if "/v1/info" in str(url):
            resp.read = MagicMock(return_value=b'{"ok":true}')
            return resp

        if method == "GET" and "/messages" in str(url):
            resp.read = MagicMock(
                return_value=json.dumps({"items": messages}).encode()
            )
            return resp

        if method == "POST" and "/messages" in str(url):
            body = getattr(req, "data", b"")
            posts.append(json.loads(body.decode()))
            return resp

        raise AssertionError(f"unexpected request: {method} {url}")

    return fake_urlopen


def test_send_posts_to_chat_endpoint(isolated_beeper: Path) -> None:
    posts: list[dict[str, Any]] = []
    urlopen = _fake_beeper_urlopen(posts)

    ok = beeper.send_message("hello", urlopen=urlopen)

    assert ok is True
    assert len(posts) == 1
    assert posts[0]["text"] == "hello"


def test_send_wraps_report_progress_in_code_block(isolated_beeper: Path) -> None:
    posts: list[dict[str, Any]] = []
    urlopen = _fake_beeper_urlopen(posts)
    body = "Progress\n  Playlists      1 / 2\n\nLast 24 hours\n  Tracks: 1"

    ok = beeper.send_report(title="daily", body=body, urlopen=urlopen)

    assert ok is True
    text = posts[0]["text"]
    assert "```" in text
    assert "Progress" in text
    assert "Playlists" in text


def test_beeper_unreachable_logs_and_records_state(isolated_beeper: Path) -> None:
    urlopen = _fake_beeper_urlopen(info_ok=False)

    ok = beeper.send_message("hello", urlopen=urlopen)

    assert ok is False
    state = agent.load_state()
    assert state.get("last_beeper_error")


def test_missing_token_helpful_no_traceback(
    isolated_beeper: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from djsync.report import run_report

    monkeypatch.setenv("DJSYNC_BEEPER_TOKEN", "")
    dest = Destination(
        drive=isolated_beeper / "drive",
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    dest.drive.mkdir()
    downloads.record_download(now=datetime.now(UTC))

    code = run_report(beeper_post=True, dest=dest)

    out = capsys.readouterr().out
    assert code == 1
    assert "DJSYNC_BEEPER_TOKEN" in out
    assert "beeper-auth" in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pause", ("pause",)),
        ("PAUSE", ("pause",)),
        ("resume", ("resume",)),
        ("status", ("status",)),
        ("cap 400", ("cap", "400")),
        ("skip forever music", ("skip", "forever music")),
        ("unskip forever music", ("unskip", "forever music")),
        ("sync House", ("sync", "House")),
    ],
)
def test_command_parsing(text: str, expected: tuple[str, ...]) -> None:
    assert beeper.parse_command(text) == expected


def test_unrecognised_message_ignored(isolated_beeper: Path) -> None:
    posts: list[dict[str, Any]] = []
    urlopen = _fake_beeper_urlopen(
        posts,
        messages=[{"id": "1", "text": "hey everyone"}],
    )
    agent.save_state({"beeper_last_message_id": "0"})

    beeper.process_incoming_commands(urlopen=urlopen)

    assert posts == []
    assert agent.load_state()["beeper_last_message_id"] == "1"


def test_command_replies_and_applies(isolated_beeper: Path) -> None:
    posts: list[dict[str, Any]] = []
    urlopen = _fake_beeper_urlopen(
        posts,
        messages=[{"id": "10", "text": "cap 400"}],
    )
    agent.save_state({"beeper_last_message_id": "9"})

    beeper.process_incoming_commands(urlopen=urlopen)

    assert len(posts) == 1
    assert posts[0]["text"] == "cap set to 400"
    assert agent.load_state()["daily_cap_override"] == 400


def test_messages_processed_once(isolated_beeper: Path) -> None:
    posts: list[dict[str, Any]] = []
    urlopen = _fake_beeper_urlopen(
        posts,
        messages=[{"id": "5", "text": "pause"}],
    )
    agent.save_state({"beeper_last_message_id": "4"})

    beeper.process_incoming_commands(urlopen=urlopen)
    beeper.process_incoming_commands(urlopen=urlopen)

    assert len(posts) == 1
    assert agent.load_state()["beeper_last_message_id"] == "5"


def test_events_deduplicated_across_runs(isolated_beeper: Path) -> None:
    posts: list[dict[str, Any]] = []
    urlopen = _fake_beeper_urlopen(posts)
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    agent.save_state(
        {
            "stop_reason": "circuit_breaker",
            "last_error": "5 consecutive download failures",
        },
        now=now,
    )

    beeper.check_and_announce_events(data=None, lockout=None, now=now, urlopen=urlopen)
    beeper.check_and_announce_events(data=None, lockout=None, now=now, urlopen=urlopen)

    assert len(posts) == 1
    assert "Circuit breaker" in posts[0]["text"]


def test_lockout_reannounces_after_clear(isolated_beeper: Path) -> None:
    posts: list[dict[str, Any]] = []
    urlopen = _fake_beeper_urlopen(posts)
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    reset1 = (now + timedelta(hours=1)).isoformat()
    lockout1 = {"reason": "QUOTA", "reset_at": reset1, "retry_after_seconds": 3600}

    beeper.check_and_announce_events(
        data=None, lockout=lockout1, now=now, urlopen=urlopen
    )
    beeper.check_and_announce_events(
        data=None, lockout=lockout1, now=now, urlopen=urlopen
    )
    assert len(posts) == 1

    reset2 = (now + timedelta(hours=2)).isoformat()
    lockout2 = {"reason": "QUOTA", "reset_at": reset2, "retry_after_seconds": 7200}
    beeper.check_and_announce_events(
        data=None,
        lockout=lockout2,
        now=now + timedelta(hours=3),
        urlopen=urlopen,
    )
    assert len(posts) == 2


def test_skip_command_persists(isolated_beeper: Path) -> None:
    posts: list[dict[str, Any]] = []
    urlopen = _fake_beeper_urlopen(
        posts,
        messages=[{"id": "2", "text": "skip forever music"}],
    )
    agent.save_state({"beeper_last_message_id": "1"})

    beeper.process_incoming_commands(urlopen=urlopen)

    assert "forever music" in agent.load_state()["skip_list"]



def test_agent_continues_when_beeper_unreachable(
    isolated_beeper: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downloads must not depend on Beeper being up."""
    dest = Destination(
        drive=isolated_beeper / "drive",
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    dest.drive.mkdir(parents=True)
    (dest.drive / "dj" / "playlists").mkdir(parents=True)
    (dest.drive / "dj" / "albums").mkdir(parents=True)
    monkeypatch.setattr(agent, "LOCK_PATH", isolated_beeper / "agent.lock")
    monkeypatch.setattr(downloads, "DAILY_DOWNLOAD_CAP", 800)

    def ismount(path: str | Path) -> bool:
        return Path(path) == dest.drive

    monkeypatch.setattr("os.path.ismount", ismount)

    cache.save_cache(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "playlist_catalog": [],
            "playlists": [
                {
                    "id": "p1",
                    "name": "Work",
                    "sigils": ["d"],
                    "track_count": 2,
                    "downloaded_count": 0,
                    "tracks": [
                        {
                            "id": "t1",
                            "name": "T",
                            "artists": ["A"],
                            "album": "Al",
                            "duration_ms": 1,
                            "isrc": None,
                            "added_at": "2024-01-01T00:00:00Z",
                            "artist_ids": [],
                            "explicit": False,
                        }
                    ],
                }
            ],
            "albums": [],
            "album_tracks": {},
            "collections": {"playlists": {"status": "ok"}, "albums": {"status": "ok"}},
        }
    )

    sync_playlist = MagicMock(return_value=SyncResult(downloaded=1))
    monkeypatch.setattr(agent, "sync_playlist", sync_playlist)
    monkeypatch.setattr(agent, "sync_album", MagicMock())
    monkeypatch.setattr(agent, "apply_local_progress", lambda *a, **k: None)
    monkeypatch.setattr(beeper, "beeper_reachable", lambda **_k: False)

    code = agent.run_agent(dest=dest, max_downloads=1, notify=lambda _m: None)

    assert code == 0
    sync_playlist.assert_called()
    assert agent.load_state().get("last_beeper_error")


def test_agent_honours_pause_and_skip(
    isolated_beeper: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = Destination(
        drive=isolated_beeper / "drive",
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )
    dest.drive.mkdir(parents=True)
    (dest.drive / "dj" / "playlists").mkdir(parents=True)
    (dest.drive / "dj" / "albums").mkdir(parents=True)
    monkeypatch.setattr(agent, "LOCK_PATH", isolated_beeper / "agent.lock")
    monkeypatch.setattr(downloads, "DAILY_DOWNLOAD_CAP", 800)

    def ismount(path: str | Path) -> bool:
        return Path(path) == dest.drive

    monkeypatch.setattr("os.path.ismount", ismount)

    cache.save_cache(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "playlist_catalog": [],
            "playlists": [
                {
                    "id": "p1",
                    "name": "Work",
                    "sigils": ["d"],
                    "track_count": 4,
                    "downloaded_count": 0,
                    "tracks": [],
                },
                {
                    "id": "p2",
                    "name": "Skipped",
                    "sigils": ["d"],
                    "track_count": 4,
                    "downloaded_count": 0,
                    "tracks": [],
                },
            ],
            "albums": [],
            "album_tracks": {},
            "collections": {"playlists": {"status": "ok"}, "albums": {"status": "ok"}},
        }
    )

    sync_playlist = MagicMock()
    monkeypatch.setattr(agent, "sync_playlist", sync_playlist)
    monkeypatch.setattr(agent, "sync_album", MagicMock())
    monkeypatch.setattr(beeper, "process_incoming_commands", lambda **_k: None)
    monkeypatch.setattr(beeper, "check_and_announce_events", lambda **_k: None)

    agent.save_state({"paused": True})
    code = agent.run_agent(dest=dest, notify=lambda _m: None)
    assert code == 0
    sync_playlist.assert_not_called()

    queue = agent.build_work_queue(
        cache.load_cache() or {},
        skip_list=["Skipped"],
    )
    names = [item.name for item in queue]
    assert "Skipped" not in names
    assert "Work" in names
