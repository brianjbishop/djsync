"""Unattended one-shot sync agent. Deterministic; no LLM calls."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Literal

from djsync import beeper, cache, config, downloads, events, quota, spotify
from djsync.config import (
    CIRCUIT_BREAKER_FAILURES,
    DAILY_DOWNLOAD_CAP,
    PLAYLISTS_PER_RUN,
    PROJECT_ROOT,
    STALE_AFTER_HOURS,
    SYNC_ALBUMS,
    Destination,
    get_destination,
)
from djsync.library import album_folder, existing_spotify_ids, playlist_folder
from djsync.sync import SyncResult, sync_album, sync_playlist

logger = logging.getLogger(__name__)

STATE_PATH = PROJECT_ROOT / ".djsync_agent.json"
LOCK_PATH = PROJECT_ROOT / ".djsync_agent.lock"

NotifyFn = Callable[[str], None]

CaffeinatePopen = Callable[..., subprocess.Popen[Any]]
_caffeinate_popen: CaffeinatePopen = subprocess.Popen


@contextmanager
def prevent_sleep() -> Iterator[subprocess.Popen[Any] | None]:
    """Hold ``caffeinate -i`` while downloads run; always terminate on exit."""
    proc: subprocess.Popen[Any] | None = None
    try:
        proc = _caffeinate_popen(["caffeinate", "-i"])
        yield proc
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@dataclass(frozen=True)
class WorkItem:
    kind: Literal["playlist", "album"]
    id: str
    name: str
    missing: int
    downloaded: int
    total: int


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_state() -> dict[str, Any]:
    """Return persisted agent status, or an empty shell."""
    if not STATE_PATH.is_file():
        return {
            "last_run": None,
            "last_error": None,
            "last_success": None,
            "discord_announced": [],
            "beeper_announced": [],
            "drive_unmounted_since": None,
            "paused": False,
            "skip_list": [],
            "sync_priority": None,
            "beeper_last_message_id": None,
        }
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "last_run": None,
            "last_error": None,
            "last_success": None,
            "discord_announced": [],
            "beeper_announced": [],
            "drive_unmounted_since": None,
            "paused": False,
            "skip_list": [],
            "sync_priority": None,
            "beeper_last_message_id": None,
        }
    if not isinstance(data, dict):
        return {
            "last_run": None,
            "last_error": None,
            "last_success": None,
            "discord_announced": [],
            "beeper_announced": [],
            "drive_unmounted_since": None,
            "paused": False,
            "skip_list": [],
            "sync_priority": None,
            "beeper_last_message_id": None,
        }
    data.setdefault("last_run", None)
    data.setdefault("last_error", None)
    data.setdefault("last_success", None)
    data.setdefault("discord_announced", [])
    data.setdefault("beeper_announced", [])
    data.setdefault("drive_unmounted_since", None)
    data.setdefault("paused", False)
    data.setdefault("skip_list", [])
    data.setdefault("sync_priority", None)
    data.setdefault("beeper_last_message_id", None)
    return data


def save_state(update: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    data = load_state()
    data.update(update)
    data["last_run"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


@contextmanager
def acquire_lock(path: Path | None = None) -> Iterator[bool]:
    """Non-blocking exclusive lock. Yields False if another run holds it."""
    lock_path = path or LOCK_PATH
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def macos_notify(message: str) -> None:
    """Show a one-line macOS notification. Never raise."""
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{escaped}" with title "djsync"',
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("notification failed", exc_info=True)


def _sleep_between_downloads() -> None:
    lo, hi = config.SLEEP_BETWEEN_DOWNLOADS
    time.sleep(random.uniform(lo, hi))


def cache_is_stale(
    data: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_after_hours: float | None = None,
) -> bool:
    now = now or _now()
    hours = STALE_AFTER_HOURS if stale_after_hours is None else stale_after_hours
    if not data or not data.get("timestamp"):
        return True
    ts = _parse_ts(str(data["timestamp"]))
    if ts is None:
        return True
    return now - ts > timedelta(hours=hours)


def hours_until_refresh(
    data: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> int:
    now = now or _now()
    if not data or not data.get("timestamp"):
        return 0
    ts = _parse_ts(str(data["timestamp"]))
    if ts is None:
        return 0
    remaining = timedelta(hours=STALE_AFTER_HOURS) - (now - ts)
    return max(0, int(remaining.total_seconds() // 3600))


def library_progress(data: dict[str, Any] | None) -> tuple[int, int]:
    """Return (downloaded, total) across $d playlists and saved albums."""
    if not data:
        return 0, 0
    have = 0
    total = 0
    for entry in data.get("playlists") or []:
        if "d" not in (entry.get("sigils") or []):
            continue
        count = int(entry.get("track_count") or 0)
        downloaded = int(entry.get("downloaded_count") or 0)
        total += count
        have += min(downloaded, count)
    for entry in data.get("albums") or []:
        count = int(entry.get("total_tracks") or 0)
        downloaded = int(entry.get("downloaded_count") or 0)
        total += count
        have += min(downloaded, count)
    return have, total


def format_status_line(data: dict[str, Any] | None, *, now: datetime | None = None) -> str:
    now = now or _now()
    have, total = library_progress(data)
    hours = hours_until_refresh(data, now=now)
    refresh = "now" if hours == 0 else f"{hours}h"
    cap = downloads.effective_daily_cap()
    state = load_state()
    paused = " · paused" if state.get("paused") else ""
    return (
        f"djsync — {have:,} of {total:,} tracks · "
        f"{cap}/day · next refresh in {refresh}{paused}"
    )


def apply_local_progress(data: dict[str, Any], dest: Destination) -> None:
    """Update cache download counts from files on the drive (additive, read-only)."""
    if not dest.mounted:
        return
    for entry in data.get("playlists") or []:
        tracks = cache.cached_playlist_tracks(entry) or []
        folder = playlist_folder(str(entry.get("name") or ""), dest.path)
        local = existing_spotify_ids(folder)
        ids = {track.id for track in tracks}
        if ids:
            entry["downloaded_count"] = len(local & ids)
    for entry in data.get("albums") or []:
        album_id = str(entry.get("id") or "")
        album_name = str(entry.get("name") or "")
        tracks = cache.cached_album_tracks(data, album_id, album_name=album_name) or []
        artists = tuple(entry.get("artists") or ())
        folder = album_folder(artists, album_name, dest.path_for("albums"))
        local = existing_spotify_ids(folder)
        ids = {track.id for track in tracks}
        if ids:
            entry["downloaded_count"] = len(local & ids)


def build_work_queue(
    data: dict[str, Any],
    *,
    skip_list: list[str] | None = None,
    sync_priority: str | None = None,
) -> list[WorkItem]:
    """$d playlists and saved albums with missing tracks, partial then smaller."""
    skip_fold = {str(s).casefold() for s in (skip_list or [])}
    sync_fold = sync_priority.casefold() if sync_priority else None
    items: list[WorkItem] = []
    for entry in data.get("playlists") or []:
        if "d" not in (entry.get("sigils") or []):
            continue
        name = str(entry.get("name") or "")
        if name.casefold() in skip_fold:
            continue
        total = int(entry.get("track_count") or 0)
        downloaded = int(entry.get("downloaded_count") or 0)
        missing = max(0, total - downloaded)
        if missing <= 0:
            continue
        items.append(
            WorkItem(
                kind="playlist",
                id=str(entry["id"]),
                name=name,
                missing=missing,
                downloaded=downloaded,
                total=total,
            )
        )
    if SYNC_ALBUMS:
        for entry in data.get("albums") or []:
            name = str(entry.get("name") or "")
            if name.casefold() in skip_fold:
                continue
            total = int(entry.get("total_tracks") or 0)
            downloaded = int(entry.get("downloaded_count") or 0)
            missing = max(0, total - downloaded)
            if missing <= 0:
                continue
            items.append(
                WorkItem(
                    kind="album",
                    id=str(entry["id"]),
                    name=name,
                    missing=missing,
                    downloaded=downloaded,
                    total=total,
                )
            )
    items.sort(
        key=lambda item: (
            0 if item.downloaded > 0 else 1,
            item.missing,
            item.total,
            item.name.casefold(),
        )
    )
    if sync_fold:
        priority = [item for item in items if item.name.casefold() == sync_fold]
        rest = [item for item in items if item.name.casefold() != sync_fold]
        items = priority + rest
    return items


def refresh_cache(*, max_playlists: int | None = None) -> dict[str, Any]:
    """Rebuild the library cache from Spotify. Isolated for tests."""
    client = spotify.get_client()
    prior = cache.load_cache()
    data = cache.build_cache(
        client,
        prior=prior,
        max_playlists=PLAYLISTS_PER_RUN if max_playlists is None else max_playlists,
        sync_albums=SYNC_ALBUMS,
    )
    cache.save_cache(data)
    return data


def _maybe_refresh(
    data: dict[str, Any] | None,
    *,
    now: datetime,
    dry_run: bool,
) -> dict[str, Any] | None:
    if dry_run or not cache_is_stale(data, now=now):
        return data
    remaining = max(0, quota.DAILY_REQUEST_BUDGET - quota.used_last_24h(now=now))
    batch = quota.max_playlists_fitting_budget(
        data,
        remaining=remaining,
        desired=PLAYLISTS_PER_RUN,
        now=now,
    )
    if batch <= 0:
        logger.info(
            "cache stale but remaining budget (%s requests) cannot cover a refresh; "
            "downloading from cache",
            remaining,
        )
        return data
    cost = quota.estimate_refresh_cost(data, max_playlists=batch, now=now)
    # Affordability, not rate: the burst ceiling is enforced per request.
    if not quota.can_spend(cost, now=now, burst=False):
        logger.info("cache stale but refresh cost %s exceeds quota; skipping", cost)
        return data
    logger.info(
        "refreshing %s of %s playlists (budget: %s requests remaining)",
        batch,
        PLAYLISTS_PER_RUN,
        remaining,
    )
    return refresh_cache(max_playlists=batch)


def _playlist_entry(data: dict[str, Any], item: WorkItem) -> dict[str, Any] | None:
    for entry in data.get("playlists") or []:
        if entry.get("id") == item.id:
            return entry
    return None


def _album_obj(data: dict[str, Any], item: WorkItem) -> Any:
    for entry in data.get("albums") or []:
        if entry.get("id") == item.id:
            return cache.album_from_entry(entry)
    return None


def _sync_one_track(
    data: dict[str, Any],
    item: WorkItem,
    *,
    dry_run: bool,
) -> SyncResult:
    client = object()
    if item.kind == "playlist":
        entry = _playlist_entry(data, item)
        if entry is None:
            return SyncResult(failed=1)
        playlist = cache.playlist_from_entry(entry)
        tracks = cache.cached_playlist_tracks(entry)
        if tracks is None:
            return SyncResult(failed=1)
        return sync_playlist(
            client,  # type: ignore[arg-type]
            playlist,
            dry_run=dry_run,
            limit=1,
            tracks=tracks,
            cached=data,
        )
    album = _album_obj(data, item)
    if album is None:
        return SyncResult(failed=1)
    tracks = cache.cached_album_tracks(data, item.id, album_name=item.name)
    if tracks is None:
        return SyncResult(failed=1)
    return sync_album(
        client,  # type: ignore[arg-type]
        album,
        dry_run=dry_run,
        limit=1,
        tracks=tracks,
        cached=data,
    )


def run_agent(
    *,
    dry_run: bool = False,
    max_downloads: int | None = None,
    dest: Destination | None = None,
    notify: NotifyFn | None = None,
    now: datetime | None = None,
) -> int:
    """One-shot agent. Never raises; returns a process exit code."""
    notify = notify or macos_notify
    now = now or _now()
    try:
        return _run_agent(
            dry_run=dry_run,
            max_downloads=max_downloads,
            dest=dest or get_destination(),
            notify=notify,
            now=now,
        )
    except Exception:
        logger.exception("agent crashed")
        try:
            save_state({"last_error": "agent crashed"}, now=now)
        except Exception:
            logger.exception("failed to persist agent error state")
        return 1


def _run_agent(
    *,
    dry_run: bool,
    max_downloads: int | None,
    dest: Destination,
    notify: NotifyFn,
    now: datetime,
) -> int:
    with acquire_lock() as held:
        if not held:
            logger.info("another agent run holds the lock; exiting")
            return 0
        return _run_locked(
            dry_run=dry_run,
            max_downloads=max_downloads,
            dest=dest,
            notify=notify,
            now=now,
        )


def _run_locked(
    *,
    dry_run: bool,
    max_downloads: int | None,
    dest: Destination,
    notify: NotifyFn,
    now: datetime,
) -> int:
    if not dest.mounted:
        state = load_state()
        if not state.get("drive_unmounted_since"):
            save_state({"drive_unmounted_since": now.isoformat()}, now=now)
        logger.info("drive not mounted; exiting")
        return 0

    save_state({"drive_unmounted_since": None}, now=now)

    beeper.process_incoming_commands(now=now)
    if load_state().get("paused"):
        logger.info("agent paused via Beeper; exiting")
        return 0

    lockout = quota.get_lockout(now=now)
    if lockout is not None:
        reason = str(lockout.get("reason") or "Spotify lockout")
        save_state({"last_error": reason, "stop_reason": "lockout"}, now=now)
        notify(f"djsync — Spotify lockout ({reason})")
        beeper.check_and_announce_events(data=cache.load_cache(), lockout=lockout, now=now)
        return 0

    data = cache.load_cache()
    try:
        data = _maybe_refresh(data, now=now, dry_run=dry_run)
    except spotify.RateLimitedError as exc:
        # Refresh recorded the lockout and saved partial work; keep going so
        # any already-cached playlists with missing files still download.
        logger.warning("refresh hit rate limit (%s); continuing from cache", exc)
        save_state({"last_error": str(exc)}, now=now)
        notify("djsync — Spotify lockout during refresh; downloading from cache")
        data = cache.load_cache() or data
    except Exception as exc:
        logger.exception("refresh failed; continuing with existing cache")
        save_state({"last_error": f"refresh failed: {exc}"}, now=now)
        data = cache.load_cache()

    if data is None:
        save_state({"last_error": None, "stop_reason": "no_cache"}, now=now)
        return 0

    apply_local_progress(data, dest)
    state = load_state()
    queue = build_work_queue(
        data,
        skip_list=list(state.get("skip_list") or []),
        sync_priority=state.get("sync_priority"),
    )
    if state.get("sync_priority") and not any(
        item.name.casefold() == str(state.get("sync_priority")).casefold()
        for item in queue
    ):
        save_state({"sync_priority": None}, now=now)

    if dry_run:
        for item in queue:
            logger.info(
                "PLAN %s %s (%s missing)",
                item.kind,
                item.name,
                item.missing,
            )
        save_state(
            {"last_error": None, "stop_reason": "dry_run", "planned": len(queue)},
            now=now,
        )
        return 0

    remaining = downloads.remaining_today(now=now)
    if max_downloads is not None:
        remaining = min(remaining, max(0, max_downloads))

    if queue and remaining > 0:
        notify(format_status_line(data, now=now))

    downloaded_this_run = 0
    consecutive_failures = 0
    stop_reason: str | None = None
    last_error: str | None = None

    if queue and remaining > 0:
        with prevent_sleep():
            for item in queue:
                if remaining <= 0:
                    stop_reason = "daily_cap"
                    break
                if consecutive_failures >= CIRCUIT_BREAKER_FAILURES:
                    break
                while item.missing > 0 and remaining > 0:
                    if not dest.mounted:
                        stop_reason = "drive_unmounted"
                        last_error = None
                        break
                    if consecutive_failures >= CIRCUIT_BREAKER_FAILURES:
                        break
                    result = _sync_one_track(data, item, dry_run=False)
                    if result.downloaded > 0:
                        took = min(result.downloaded, remaining)
                        for _ in range(took):
                            downloads.record_download(now=now)
                        downloaded_this_run += took
                        remaining -= took
                        item = WorkItem(
                            kind=item.kind,
                            id=item.id,
                            name=item.name,
                            missing=max(0, item.missing - took),
                            downloaded=item.downloaded + took,
                            total=item.total,
                        )
                        consecutive_failures = 0
                        last_error = None
                        for entry in result.unverified_explicit:
                            events.record_unverified_explicit(entry, now=now)
                        if item.missing == 0:
                            events.record_completion(
                                item.kind,
                                item.id,
                                item.name,
                                now=now,
                            )
                    elif result.failed > 0:
                        consecutive_failures += result.failed
                        failure_reason = f"{result.failed} download failure(s)"
                        events.record_failure(failure_reason, now=now)
                        last_error = (
                            f"circuit breaker: {CIRCUIT_BREAKER_FAILURES} consecutive "
                            "download failures"
                            if consecutive_failures >= CIRCUIT_BREAKER_FAILURES
                            else failure_reason
                        )
                        if consecutive_failures >= CIRCUIT_BREAKER_FAILURES:
                            events.record_failure(last_error, circuit_breaker=True, now=now)
                            stop_reason = "circuit_breaker"
                            break
                    else:
                        item = WorkItem(
                            kind=item.kind,
                            id=item.id,
                            name=item.name,
                            missing=0,
                            downloaded=item.downloaded,
                            total=item.total,
                        )
                        break
                    if item.missing > 0 and remaining > 0:
                        _sleep_between_downloads()
                if stop_reason in ("drive_unmounted", "circuit_breaker"):
                    break

    apply_local_progress(data, dest)

    if stop_reason == "circuit_breaker":
        save_state(
            {
                "last_error": last_error,
                "stop_reason": stop_reason,
                "downloaded": downloaded_this_run,
            },
            now=now,
        )
        notify(f"djsync — {last_error}")
        beeper.check_and_announce_events(data=data, lockout=None, now=now)
        return 0

    success_at = now.isoformat() if downloaded_this_run else load_state().get("last_success")
    save_state(
        {
            "last_error": None if stop_reason != "drive_unmounted" else last_error,
            "last_success": success_at,
            "stop_reason": stop_reason or ("done" if downloaded_this_run else "idle"),
            "downloaded": downloaded_this_run,
        },
        now=now,
    )
    if downloaded_this_run:
        have, total = library_progress(data)
        left = downloads.remaining_today(now=now)
        notify(
            f"djsync — downloaded {downloaded_this_run} tracks · "
            f"{have:,} of {total:,} · {left} left today"
        )
    beeper.check_and_announce_events(data=data, lockout=None, now=now)
    return 0
