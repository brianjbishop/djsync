"""Track YouTube downloads and enforce a rolling 24h local cap."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from djsync.config import DAILY_DOWNLOAD_CAP, PROJECT_ROOT

LEDGER_PATH = PROJECT_ROOT / ".djsync_downloads.json"

_WINDOW_24H = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _load_ledger() -> dict[str, Any]:
    if not LEDGER_PATH.is_file():
        return {"downloads": []}
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"downloads": []}
    if not isinstance(data.get("downloads"), list):
        data["downloads"] = []
    return data


def _save_ledger(data: dict[str, Any]) -> None:
    LEDGER_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _prune_downloads(data: dict[str, Any], *, now: datetime | None = None) -> list[str]:
    now = now or _now()
    cutoff = now - _WINDOW_24H
    kept: list[str] = []
    for raw in data.get("downloads") or []:
        if not isinstance(raw, str):
            continue
        try:
            ts = _parse_ts(raw)
        except ValueError:
            continue
        if ts >= cutoff:
            kept.append(raw)
    data["downloads"] = kept
    return kept


def record_download(*, now: datetime | None = None) -> None:
    """Append one successful YouTube download to the ledger."""
    now = now or _now()
    data = _load_ledger()
    _prune_downloads(data, now=now)
    data["downloads"].append(now.isoformat())
    _save_ledger(data)


def used_last_24h(*, now: datetime | None = None) -> int:
    data = _load_ledger()
    return len(_prune_downloads(data, now=now))


def effective_daily_cap() -> int:
    """Daily download cap, including any Beeper ``cap`` command override."""
    from djsync import agent

    override = agent.load_state().get("daily_cap_override")
    if override is not None:
        try:
            return int(override)
        except (TypeError, ValueError):
            pass
    return DAILY_DOWNLOAD_CAP


def remaining_today(*, now: datetime | None = None) -> int:
    used = used_last_24h(now=now)
    return max(0, effective_daily_cap() - used)


def can_download(n: int, *, now: datetime | None = None) -> bool:
    """Return True if *n* downloads are allowed under the rolling 24h cap."""
    if n <= 0:
        return True
    return remaining_today(now=now) >= n
