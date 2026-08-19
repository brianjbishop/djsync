"""Persist match attempts for offline regression testing."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from djsync.config import WEIGHTS
from djsync.matcher.candidate import Candidate
from djsync.models import Track


def _weights_version() -> str:
    payload = json.dumps(WEIGHTS, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _candidate_payload(cand: Candidate) -> dict[str, Any]:
    return {
        "video_id": cand.video_id,
        "url": cand.url,
        "title": cand.title,
        "channel": cand.channel,
        "duration_s": cand.duration_s,
        "view_count": cand.view_count,
        "is_art_track": cand.is_art_track,
        "description": cand.description,
    }


def _track_payload(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "name": track.name,
        "artists": list(track.artists),
        "album": track.album,
        "duration_ms": track.duration_ms,
        "isrc": track.isrc,
        "explicit": track.explicit,
    }


def record_match(
    track: Track,
    candidates: list[Candidate],
    chosen: Candidate | None,
    scores: list[tuple[Candidate, float, dict[str, float]]],
    fixtures_dir: Path,
) -> Path:
    """Write one JSON fixture file for a match attempt."""
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    path = fixtures_dir / f"{track.id}.json"

    payload = {
        "spotify": _track_payload(track),
        "candidates": [_candidate_payload(c) for c in candidates],
        "scores": [
            {
                "video_id": cand.video_id,
                "total": total,
                "breakdown": breakdown,
            }
            for cand, total, breakdown in scores
        ],
        "chosen_video_id": chosen.video_id if chosen else None,
        "timestamp": datetime.now(UTC).isoformat(),
        "weights_version": _weights_version(),
    }

    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path
