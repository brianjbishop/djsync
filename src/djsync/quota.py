"""Track Spotify API usage and enforce local request budgets."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from djsync.config import BURST_PER_30S, DAILY_REQUEST_BUDGET, PROJECT_ROOT

LEDGER_PATH = PROJECT_ROOT / ".djsync_quota.json"

_WINDOW_24H = timedelta(hours=24)
_WINDOW_30S = timedelta(seconds=30)


class QuotaBudgetError(Exception):
    """Raised when a call would exceed the configured local request budget."""


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _load_ledger() -> dict[str, Any]:
    if not LEDGER_PATH.is_file():
        return {"requests": [], "lockout": None}
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"requests": [], "lockout": None}
    if not isinstance(data.get("requests"), list):
        data["requests"] = []
    if "lockout" not in data:
        data["lockout"] = None
    return data


def _save_ledger(data: dict[str, Any]) -> None:
    LEDGER_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _prune_requests(data: dict[str, Any], *, now: datetime | None = None) -> list[str]:
    """Drop request timestamps older than 24h; return the surviving ISO strings."""
    now = now or _now()
    cutoff = now - _WINDOW_24H
    kept: list[str] = []
    for raw in data.get("requests") or []:
        if not isinstance(raw, str):
            continue
        try:
            ts = _parse_ts(raw)
        except ValueError:
            continue
        if ts >= cutoff:
            kept.append(raw)
    data["requests"] = kept
    return kept


def _active_lockout(data: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or _now()
    lockout = data.get("lockout")
    if not isinstance(lockout, dict):
        data["lockout"] = None
        return None
    reset_at = lockout.get("reset_at")
    if not reset_at:
        data["lockout"] = None
        return None
    try:
        reset = _parse_ts(str(reset_at))
    except ValueError:
        data["lockout"] = None
        return None
    if reset <= now:
        data["lockout"] = None
        return None
    return lockout


def record_request(*, now: datetime | None = None) -> None:
    """Append one successful Spotify request to the ledger."""
    now = now or _now()
    data = _load_ledger()
    _prune_requests(data, now=now)
    data["requests"].append(now.isoformat())
    _save_ledger(data)


def record_429(
    reason: str,
    retry_after_seconds: int,
    *,
    now: datetime | None = None,
) -> None:
    """Persist a Spotify 429 lockout and count the request."""
    now = now or _now()
    data = _load_ledger()
    _prune_requests(data, now=now)
    data["requests"].append(now.isoformat())
    reset = now + timedelta(seconds=max(0, retry_after_seconds))
    data["lockout"] = {
        "reason": reason,
        "retry_after_seconds": retry_after_seconds,
        "reset_at": reset.isoformat(),
    }
    _save_ledger(data)


def get_lockout(*, now: datetime | None = None) -> dict[str, Any] | None:
    """Return the active lockout payload, or None if expired/absent."""
    now = now or _now()
    data = _load_ledger()
    lockout = _active_lockout(data, now=now)
    _save_ledger(data)
    return lockout


def used_last_24h(*, now: datetime | None = None) -> int:
    data = _load_ledger()
    return len(_prune_requests(data, now=now))


def used_last_30s(*, now: datetime | None = None) -> int:
    now = now or _now()
    data = _load_ledger()
    _prune_requests(data, now=now)
    cutoff = now - _WINDOW_30S
    count = 0
    for raw in data.get("requests") or []:
        try:
            ts = _parse_ts(raw)
        except ValueError:
            continue
        if ts >= cutoff:
            count += 1
    return count


def can_spend(n: int, *, now: datetime | None = None, burst: bool = True) -> bool:
    """Return True if *n* requests are allowed under lockout and budget rules.

    ``burst`` applies the 30-second rate ceiling. Leave it on for a single
    request about to be made. Turn it OFF when asking whether a multi-request
    operation is affordable overall: BURST_PER_30S is a rate, not a total, and
    a batch is spread over minutes by per-request pacing. Applying it to the
    whole batch makes any operation larger than the burst limit permanently
    impossible however much daily budget remains.
    """
    if n <= 0:
        return True
    now = now or _now()
    data = _load_ledger()
    if _active_lockout(data, now=now) is not None:
        return False
    used_24h = len(_prune_requests(data, now=now))
    if used_24h + n > DAILY_REQUEST_BUDGET:
        return False
    if burst and used_last_30s(now=now) + n > BURST_PER_30S:
        return False
    return True


def wait_for_burst_capacity(n: int = 1, *, sleep=None, now_fn=None) -> float:
    """Block until *n* requests fit inside the 30s burst window.

    Exceeding a RATE limit means wait, not fail. Only a lockout or the daily
    budget are refusals. Returns the seconds actually slept (0 if none needed).
    """
    import time as _time

    sleeper = sleep or _time.sleep
    clock = now_fn or _now
    slept = 0.0
    # Bounded: one burst window plus slack. A caller that cannot get capacity
    # in that time has a real problem, and spinning here would hide it.
    deadline = 60.0
    while slept < deadline:
        now = clock()
        data = _load_ledger()
        _prune_requests(data, now=now)
        recent = []
        for raw in data.get("requests") or []:
            ts = _parse_ts(raw)
            if ts is not None and (now - ts).total_seconds() < 30:
                recent.append(ts)
        recent.sort()
        if len(recent) + n <= BURST_PER_30S:
            return slept
        # Wait just past the moment the oldest in-window request ages out.
        wait = 30.0 - (now - recent[0]).total_seconds() + 0.25
        wait = max(0.25, min(wait, 30.0))
        sleeper(wait)
        slept += wait
    return slept


def check_can_spend(n: int, *, now: datetime | None = None) -> None:
    """Raise if *n* requests cannot be spent (lockout or budget)."""
    from djsync.spotify import RateLimitedError, format_rate_limit_message

    now = now or _now()
    data = _load_ledger()
    lockout = _active_lockout(data, now=now)
    _save_ledger(data)
    if lockout is not None:
        retry_after = int(lockout.get("retry_after_seconds") or 0)
        reason = lockout.get("reason") or "QUOTA_EXCEEDED"
        msg = format_rate_limit_message(retry_after)
        if reason:
            msg = f"{reason}: {msg}"
        raise RateLimitedError(retry_after, message=msg)

    if not can_spend(n, now=now):
        used = used_last_24h(now=now)
        if used + n > DAILY_REQUEST_BUDGET:
            raise QuotaBudgetError(
                f"Daily Spotify request budget exceeded ({used}/{DAILY_REQUEST_BUDGET})."
            )
        raise QuotaBudgetError(
            f"Burst limit exceeded ({used_last_30s(now=now)}/{BURST_PER_30S} in 30s)."
        )


def quota_status(*, now: datetime | None = None) -> dict[str, Any]:
    """Return ledger counters and any active lockout for the UI."""
    now = now or _now()
    lockout = get_lockout(now=now)
    return {
        "used_24h": used_last_24h(now=now),
        "daily_budget": DAILY_REQUEST_BUDGET,
        "used_30s": used_last_30s(now=now),
        "burst_30s": BURST_PER_30S,
        "lockout": lockout,
    }


def estimate_refresh_cost(
    prior: dict[str, Any] | None = None,
    *,
    max_playlists: int | None = None,
) -> int:
    """Estimate Spotify requests for a full cache refresh."""
    from djsync import cache

    prior = dict(prior or {})
    catalog = prior.get("playlist_catalog") or []
    playlist_count = len(catalog)
    if playlist_count == 0:
        scanned = prior.get("playlists") or []
        playlist_count = max(len(scanned), 1)

    playlist_list_pages = max(1, (playlist_count + 49) // 50)

    marked_d = [
        cache.playlist_from_entry(entry)
        for entry in prior.get("playlists") or []
        if "d" in (entry.get("sigils") or [])
    ]
    if catalog:
        marked_d = [
            cache.playlist_from_entry(entry)
            for entry in catalog
            if "d" in (entry.get("sigils") or [])
        ]

    refetch = cache.estimate_playlist_refetches(
        marked_d,
        prior,
    )
    if max_playlists is not None:
        refetch = min(refetch, max_playlists)

    albums = prior.get("albums") or []
    album_pages = max(1, (len(albums) + 49) // 50) if albums else 1
    album_tracks_cache = prior.get("album_tracks") or {}
    new_album_tracks = sum(1 for album in albums if album.get("id") not in album_tracks_cache)

    return playlist_list_pages + refetch + album_pages + new_album_tracks
