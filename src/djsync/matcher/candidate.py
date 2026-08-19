"""YouTube match candidate datamodel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    video_id: str
    url: str
    title: str
    channel: str
    duration_s: int
    view_count: int
    is_art_track: bool
    description: str
