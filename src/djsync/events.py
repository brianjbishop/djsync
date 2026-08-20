"""Persist agent events for daily reports (failures, completions, explicit review)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from djsync.config import PROJECT_ROOT

EVENTS_PATH = PROJECT_ROOT / ".djsync_events.json"

_RETENTION = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load() -> dict[str, Any]:
    if not EVENTS_PATH.is_file():
        return {"events": []}
    try:
        data = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"events": []}
    if not isinstance(data.get("events"), list):
        data["events"] = []
    return data


def _save(data: dict[str, Any]) -> None:
    EVENTS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _prune(data: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or _now()
    cutoff = now - _RETENTION
    kept: list[dict[str, Any]] = []
    for entry in data.get("events") or []:
        if not isinstance(entry, dict):
            continue
        ts = _parse_ts(str(entry.get("ts") or ""))
        if ts is None or ts < cutoff:
            continue
        kept.append(entry)
    data["events"] = kept
    return kept


def record_failure(
    reason: str,
    *,
    circuit_breaker: bool = False,
    now: datetime | None = None,
) -> None:
    now = now or _now()
    data = _load()
    _prune(data, now=now)
    data["events"].append(
        {
            "kind": "failure",
            "ts": now.isoformat(),
            "reason": reason,
            "circuit_breaker": circuit_breaker,
        }
    )
    _save(data)


def record_completion(
    collection: str,
    item_id: str,
    name: str,
    *,
    now: datetime | None = None,
) -> None:
    now = now or _now()
    data = _load()
    _prune(data, now=now)
    data["events"].append(
        {
            "kind": "completion",
            "ts": now.isoformat(),
            "collection": collection,
            "id": item_id,
            "name": name,
        }
    )
    _save(data)


def record_unverified_explicit(
    entry: dict[str, str | list[str]],
    *,
    now: datetime | None = None,
) -> None:
    now = now or _now()
    data = _load()
    _prune(data, now=now)
    data["events"].append(
        {
            "kind": "unverified_explicit",
            "ts": now.isoformat(),
            "track_id": entry.get("track_id"),
            "name": entry.get("name"),
            "artists": entry.get("artists"),
            "chosen_title": entry.get("chosen_title"),
            "chosen_url": entry.get("chosen_url"),
        }
    )
    _save(data)


def events_since(
    hours: float,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or _now()
    cutoff = now - timedelta(hours=hours)
    data = _load()
    result: list[dict[str, Any]] = []
    for entry in _prune(data, now=now):
        ts = _parse_ts(str(entry.get("ts") or ""))
        if ts is not None and ts >= cutoff:
            result.append(entry)
    _save(data)
    return result
