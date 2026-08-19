"""Pure scoring for YouTube match candidates.

No network, no file I/O — only stdlib plus dataclass models.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

from djsync.matcher.candidate import Candidate
from djsync.models import Track

NEGATIVE_FLAGS = (
    "sped up",
    "slowed",
    "reverb",
    "8d",
    "nightcore",
    "live",
    "cover",
    "karaoke",
    "instrumental",
    "remix",
    "edit",
    "mashup",
    "loop",
    "bass boosted",
)

_CONDITIONAL_FLAGS = frozenset({"remix", "edit"})

_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_JUNK_TERMS = re.compile(
    r"\b("
    r"official video|official audio|official music video|"
    r"lyrics|hd|4k|visualizer"
    r")\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_title(text: str) -> str:
    """Lowercase, strip bracketed junk and common YouTube suffixes."""
    text = text.lower()
    text = _BRACKETED.sub(" ", text)
    text = _JUNK_TERMS.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _duration_delta_score(delta_s: float, weight: float) -> float:
    if delta_s <= 2:
        return weight
    if delta_s >= 15:
        return -weight
    t = (delta_s - 2) / 13
    return weight * (1 - 2 * t)


def _title_similarity(track: Track, cand: Candidate, weight: float) -> float:
    track_norm = normalize_title(track.name)
    cand_norm = normalize_title(cand.title)
    if not track_norm or not cand_norm:
        return 0.0
    ratio = SequenceMatcher(None, track_norm, cand_norm).ratio()
    return ratio * weight


def _artist_match(track: Track, cand: Candidate, weight: float) -> float:
    haystack = f"{cand.channel} {cand.title}".lower()
    for artist in track.artists:
        if artist.lower() in haystack:
            return weight
    return 0.0


def _negative_flags(track: Track, cand: Candidate, weight: float) -> float:
    cand_lower = cand.title.lower()
    track_lower = track.name.lower()
    penalty = 0.0
    for flag in NEGATIVE_FLAGS:
        if flag not in cand_lower:
            continue
        if flag in _CONDITIONAL_FLAGS and flag in track_lower:
            continue
        penalty += weight
    return penalty


def _view_count_score(view_count: int, weight: float) -> float:
    if view_count <= 0:
        return 0.0
    return math.log10(view_count + 1) * weight


def score_candidate(
    track: Track,
    cand: Candidate,
    weights: dict[str, float],
) -> tuple[float, dict[str, float]]:
    """Return total score and per-signal contribution breakdown."""
    delta_s = abs(cand.duration_s - track.duration_ms / 1000)

    breakdown: dict[str, float] = {
        "duration_delta": _duration_delta_score(delta_s, weights["duration_delta"]),
        "art_track": weights["art_track"] if cand.is_art_track else 0.0,
        "title_similarity": _title_similarity(track, cand, weights["title_similarity"]),
        "artist_match": _artist_match(track, cand, weights["artist_match"]),
        "negative_flags": _negative_flags(track, cand, weights["negative_flags"]),
        "view_count": _view_count_score(cand.view_count, weights["view_count"]),
    }
    total = sum(breakdown.values())
    return total, breakdown


def rank(
    track: Track,
    cands: list[Candidate],
    weights: dict[str, float],
) -> list[tuple[Candidate, float, dict[str, float]]]:
    """Score and sort candidates best-first."""
    scored = [(cand, *score_candidate(track, cand, weights)) for cand in cands]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored
