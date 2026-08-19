"""Shared datamodels with no I/O or network dependencies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    artists: tuple[str, ...]
    album: str
    duration_ms: int
    isrc: str | None
    added_at: str | None = None
    artist_ids: tuple[str, ...] = ()
    track_number: int = 0
    disc_number: int = 0
