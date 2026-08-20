"""Tests for the unattended background agent (no network, no real drive)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from djsync import agent, beeper, cache, downloads, events, quota
from djsync.config import Destination
from djsync.sync import SyncResult


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(quota, "LEDGER_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(downloads, "LEDGER_PATH", tmp_path / "downloads.json")
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.json")
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(agent, "STATE_PATH", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOCK_PATH", tmp_path / "agent.lock")
    monkeypatch.setattr(downloads, "DAILY_DOWNLOAD_CAP", 800)
    return tmp_path


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Spotify/network must not be called in agent tests")

    monkeypatch.setattr("djsync.spotify.get_client", boom)
    monkeypatch.setattr("djsync.cache.build_cache", boom)
    monkeypatch.setattr(agent, "sync_playlist", boom)
    monkeypatch.setattr(agent, "sync_album", boom)
    monkeypatch.setattr(agent, "refresh_cache", boom)
    monkeypatch.setattr(agent, "_sleep_between_downloads", lambda: None)


def _dest(tmp_path: Path, *, mounted: bool) -> Destination:
    drive = tmp_path / "drive"
    if mounted:
        drive.mkdir(parents=True, exist_ok=True)
        (drive / "dj" / "playlists").mkdir(parents=True, exist_ok=True)
        (drive / "dj" / "albums").mkdir(parents=True, exist_ok=True)
    return Destination(
        drive=drive,
        library_root="dj",
        playlists_dir="playlists",
        albums_dir="albums",
    )


def _mount(monkeypatch: pytest.MonkeyPatch, dest: Destination, mounted: bool) -> None:
    drive = dest.drive

    def ismount(path: str | os.PathLike[str]) -> bool:
        return mounted and Path(path) == drive

    monkeypatch.setattr(os.path, "ismount", ismount)


def _track_dict(track_id: str, name: str) -> dict[str, Any]:
    return {
        "id": track_id,
        "name": name,
        "artists": ["Artist"],
        "album": "Album",
        "duration_ms": 180_000,
        "isrc": None,
        "added_at": "2024-06-01T00:00:00Z",
        "artist_ids": [],
        "explicit": False,
    }


def _playlist_entry(
    *,
    playlist_id: str,
    name: str,
    downloaded: int,
    total: int,
) -> dict[str, Any]:
    tracks = [_track_dict(f"{playlist_id}-t{i}", f"Track {i}") for i in range(total)]
    status = "complete" if downloaded >= total else ("partial" if downloaded else "none")
    return {
        "id": playlist_id,
        "name": name,
        "sigils": ["d"],
        "track_count": total,
        "snapshot_id": "snap",
        "tracks": tracks,
        "downloaded_count": downloaded,
        "status": status,
        "last_added": "2024-06-01T00:00:00Z",
        "spotify_url": f"https://open.spotify.com/playlist/{playlist_id}",
        "spotify_uri": f"spotify:playlist:{playlist_id}",
    }


def _album_entry(
    *,
    album_id: str,
    name: str,
    downloaded: int,
    total: int,
) -> dict[str, Any]:
    tracks = [
        {
            **_track_dict(f"{album_id}-t{i}", f"Album Track {i}"),
            "track_number": i + 1,
            "disc_number": 1,
        }
        for i in range(total)
    ]
    status = "complete" if downloaded >= total else ("partial" if downloaded else "none")
    return {
        "id": album_id,
        "name": name,
        "artists": ["Artist"],
        "total_tracks": total,
        "downloaded_count": downloaded,
        "status": status,
        "added_at": "2024-01-01T00:00:00Z",
        "release_date": "2020-01-01",
        "spotify_url": f"https://open.spotify.com/album/{album_id}",
        "spotify_uri": f"spotify:album:{album_id}",
        "_tracks": tracks,
    }


def _cache_payload(
    playlists: list[dict[str, Any]],
    albums: list[dict[str, Any]] | None = None,
    *,
    timestamp: str = "2026-08-19T00:00:00+00:00",
) -> dict[str, Any]:
    albums = albums or []
    album_tracks = {
        entry["id"]: entry.pop("_tracks")
        for entry in albums
        if "_tracks" in entry
    }
    return {
        "timestamp": timestamp,
        "playlist_catalog": [],
        "playlists": playlists,
        "albums": albums,
        "album_tracks": album_tracks,
        "collections": {
            "playlists": {"status": "ok"},
            "albums": {"status": "ok"},
        },
    }


def test_work_queue_partial_before_untouched_then_smaller_first() -> None:
    data = _cache_payload(
        [
            _playlist_entry(
                playlist_id="big-untouched",
                name="Large untouched",
                downloaded=0,
                total=50,
            ),
            _playlist_entry(
                playlist_id="partial-big",
                name="Partial Big",
                downloaded=1,
                total=100,
            ),
            _playlist_entry(
                playlist_id="tiny",
                name="Tiny untouched",
                downloaded=0,
                total=2,
            ),
            _playlist_entry(
                playlist_id="partial-small",
                name="Partial Small",
                downloaded=1,
                total=5,
            ),
            _playlist_entry(
                playlist_id="done",
                name="Already done",
                downloaded=3,
                total=3,
            ),
        ],
        [
            _album_entry(
                album_id="alb-partial",
                name="Partial Album",
                downloaded=2,
                total=8,
            ),
        ],
    )

    names = [item.name for item in agent.build_work_queue(data)]
    assert names == [
        "Partial Small",
        "Partial Album",
        "Partial Big",
        "Tiny untouched",
        "Large untouched",
    ]


def test_agent_exits_quietly_when_drive_unmounted(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes: list[str] = []
    dest = _dest(isolated_state, mounted=False)
    _mount(monkeypatch, dest, False)
    sync_playlist = MagicMock(return_value=SyncResult(downloaded=1))
    monkeypatch.setattr(agent, "sync_playlist", sync_playlist)

    code = agent.run_agent(dest=dest, notify=notes.append)

    assert code == 0
    assert notes == []
    sync_playlist.assert_not_called()


def test_agent_exits_quietly_during_quota_lockout(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes: list[str] = []
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    quota.record_429("QUOTA_EXCEEDED", 3600, now=datetime.now(UTC))
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=4
                )
            ]
        )
    )
    sync_playlist = MagicMock(return_value=SyncResult(downloaded=1))
    monkeypatch.setattr(agent, "sync_playlist", sync_playlist)

    code = agent.run_agent(dest=dest, notify=notes.append)

    assert code == 0
    sync_playlist.assert_not_called()
    assert len(notes) == 1
    assert "lockout" in notes[0].casefold() or "quota" in notes[0].casefold()


def test_daily_download_cap_enforced_across_runs(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(downloads, "DAILY_DOWNLOAD_CAP", 3)
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=10
                )
            ],
            timestamp=datetime.now(UTC).isoformat(),
        )
    )

    def fake_sync(*_args: object, **_kwargs: object) -> SyncResult:
        return SyncResult(downloaded=1)

    monkeypatch.setattr(agent, "sync_playlist", fake_sync)
    monkeypatch.setattr(agent, "sync_album", fake_sync)

    first = agent.run_agent(dest=dest, notify=lambda _msg: None)
    second = agent.run_agent(dest=dest, notify=lambda _msg: None)

    assert first == 0
    assert second == 0
    assert downloads.used_last_24h() == 3
    assert downloads.remaining_today() == 0


def test_circuit_breaker_trips_after_consecutive_failures(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "CIRCUIT_BREAKER_FAILURES", 5)
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=20
                )
            ],
            timestamp=datetime.now(UTC).isoformat(),
        )
    )
    calls = {"n": 0}

    def fake_sync(*_args: object, **_kwargs: object) -> SyncResult:
        calls["n"] += 1
        return SyncResult(failed=1)

    monkeypatch.setattr(agent, "sync_playlist", fake_sync)
    notes: list[str] = []

    code = agent.run_agent(dest=dest, notify=notes.append)

    assert code == 0
    assert calls["n"] == 5
    state = agent.load_state()
    assert state["last_error"]
    assert "circuit" in str(state["last_error"]).casefold()
    assert notes
    assert any("circuit" in note.casefold() for note in notes)


def test_lockfile_prevents_second_concurrent_run(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=4
                )
            ],
            timestamp=datetime.now(UTC).isoformat(),
        )
    )
    sync_playlist = MagicMock(return_value=SyncResult(downloaded=1))
    monkeypatch.setattr(agent, "sync_playlist", sync_playlist)
    notes: list[str] = []

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys, time\n"
                "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "sys.stdout.write('ready\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(60)\n"
            ),
            str(agent.LOCK_PATH),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        ready = holder.stdout.readline()
        assert ready == b"ready\n"
        code = agent.run_agent(dest=dest, notify=notes.append)
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert code == 0
    sync_playlist.assert_not_called()
    assert notes == []


def test_max_overrides_per_run_allowance_but_not_daily_cap(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(downloads, "DAILY_DOWNLOAD_CAP", 50)
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=10
                )
            ],
            timestamp=datetime.now(UTC).isoformat(),
        )
    )
    monkeypatch.setattr(
        agent, "sync_playlist", lambda *_a, **_k: SyncResult(downloaded=1)
    )

    code = agent.run_agent(dest=dest, max_downloads=2, notify=lambda _msg: None)

    assert code == 0
    assert downloads.used_last_24h() == 2


def test_dry_run_does_not_download(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=4
                )
            ],
            timestamp=datetime.now(UTC).isoformat(),
        )
    )
    sync_playlist = MagicMock(return_value=SyncResult(downloaded=1))
    monkeypatch.setattr(agent, "sync_playlist", sync_playlist)
    notes: list[str] = []

    code = agent.run_agent(dest=dest, dry_run=True, notify=notes.append)

    assert code == 0
    sync_playlist.assert_not_called()
    assert downloads.used_last_24h() == 0
    assert notes == []


def test_stale_cache_skips_refresh_when_quota_cannot_cover_cost(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    old = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=2
                )
            ],
            timestamp=old,
        )
    )
    monkeypatch.setattr(quota, "can_spend", lambda *_a, **_k: False)
    refresh = MagicMock()
    monkeypatch.setattr(agent, "refresh_cache", refresh)
    monkeypatch.setattr(
        agent, "sync_playlist", lambda *_a, **_k: SyncResult(downloaded=1)
    )

    code = agent.run_agent(dest=dest, notify=lambda _msg: None)

    assert code == 0
    refresh.assert_not_called()
    assert downloads.used_last_24h() == 2


class _FakeCaffeinateProc:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


def test_caffeinate_starts_during_download_and_terminates_after(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=4
                )
            ],
            timestamp=datetime.now(UTC).isoformat(),
        )
    )
    procs: list[_FakeCaffeinateProc] = []

    def fake_popen(args: list[str], **_kwargs: object) -> _FakeCaffeinateProc:
        assert args == ["caffeinate", "-i"]
        proc = _FakeCaffeinateProc()
        procs.append(proc)
        return proc

    monkeypatch.setattr(agent, "_caffeinate_popen", fake_popen)
    monkeypatch.setattr(
        agent, "sync_playlist", lambda *_a, **_k: SyncResult(downloaded=1)
    )

    code = agent.run_agent(dest=dest, notify=lambda _msg: None)

    assert code == 0
    assert len(procs) == 1
    assert procs[0].terminated is True


def test_caffeinate_not_started_when_nothing_to_download(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=3, total=3
                )
            ],
            timestamp=datetime.now(UTC).isoformat(),
        )
    )
    popen = MagicMock()
    monkeypatch.setattr(agent, "_caffeinate_popen", popen)
    # apply_local_progress rescans the real drive and would overwrite the
    # cached counts with 0 for this temp dir, re-queueing a complete playlist.
    # This test is about caffeinate, not progress scanning.
    monkeypatch.setattr(agent, "apply_local_progress", lambda *a, **k: None)

    code = agent.run_agent(dest=dest, notify=lambda _msg: None)

    assert code == 0
    popen.assert_not_called()


def test_caffeinate_terminated_when_download_raises(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = _dest(isolated_state, mounted=True)
    _mount(monkeypatch, dest, True)
    cache.save_cache(
        _cache_payload(
            [
                _playlist_entry(
                    playlist_id="p1", name="$d Work", downloaded=0, total=4
                )
            ],
            timestamp=datetime.now(UTC).isoformat(),
        )
    )
    procs: list[_FakeCaffeinateProc] = []

    def fake_popen(args: list[str], **_kwargs: object) -> _FakeCaffeinateProc:
        proc = _FakeCaffeinateProc()
        procs.append(proc)
        return proc

    monkeypatch.setattr(agent, "_caffeinate_popen", fake_popen)

    def boom(*_args: object, **_kwargs: object) -> SyncResult:
        raise RuntimeError("download exploded")

    monkeypatch.setattr(agent, "sync_playlist", boom)

    code = agent.run_agent(dest=dest, notify=lambda _msg: None)

    assert code == 1
    assert len(procs) == 1
    assert procs[0].terminated is True


def test_beeper_events_deduplicated_across_runs(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fixed: prior webhook tests patched config after notify_discord imported the
    # URL at module load time, so delivery never ran. Beeper reads settings at runtime.
    monkeypatch.setenv("DJSYNC_BEEPER_TOKEN", "test-token")
    posts: list[dict[str, object]] = []

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
        posts.append(json.loads(body.decode()))
        return resp

    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    agent.save_state(
        {
            "stop_reason": "circuit_breaker",
            "last_error": "5 consecutive download failures",
        },
        now=now,
    )
    data = _cache_payload([_playlist_entry(playlist_id="p1", name="House", downloaded=1, total=4)])

    beeper.check_and_announce_events(
        data=data,
        lockout=None,
        now=now,
        urlopen=fake_urlopen,
    )
    beeper.check_and_announce_events(
        data=data,
        lockout=None,
        now=now,
        urlopen=fake_urlopen,
    )

    assert len(posts) == 1
    assert "Circuit breaker" in str(posts[0].get("text", ""))


def test_beeper_lockout_reannounces_after_clear(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DJSYNC_BEEPER_TOKEN", "test-token")
    posts: list[dict[str, object]] = []

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
        posts.append(json.loads(body.decode()))
        return resp

    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    reset1 = (now + timedelta(hours=1)).isoformat()
    lockout1 = {"reason": "QUOTA", "reset_at": reset1, "retry_after_seconds": 3600}

    beeper.check_and_announce_events(
        data=None,
        lockout=lockout1,
        now=now,
        urlopen=fake_urlopen,
    )
    beeper.check_and_announce_events(
        data=None,
        lockout=lockout1,
        now=now,
        urlopen=fake_urlopen,
    )
    assert len(posts) == 1

    reset2 = (now + timedelta(hours=2)).isoformat()
    lockout2 = {"reason": "QUOTA", "reset_at": reset2, "retry_after_seconds": 7200}
    beeper.check_and_announce_events(
        data=None,
        lockout=lockout2,
        now=now + timedelta(hours=3),
        urlopen=fake_urlopen,
    )
    assert len(posts) == 2
