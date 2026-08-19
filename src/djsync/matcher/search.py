"""YouTube search via yt-dlp (no API key)."""

from __future__ import annotations

from typing import Any

import yt_dlp

from djsync import config
from djsync.matcher.candidate import Candidate
from djsync.models import Track

_FLAT_OPTS: dict[str, Any] = {
    **config.youtube_opts(),
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "skip_download": True,
}

_DETAIL_OPTS: dict[str, Any] = {
    **config.youtube_opts(),
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
}


def _is_art_track(channel: str, description: str) -> bool:
    if channel.endswith(" - Topic"):
        return True
    return "Provided to YouTube by" in description


def _entry_channel(entry: dict[str, Any]) -> str:
    return (
        entry.get("channel")
        or entry.get("uploader")
        or entry.get("channel_name")
        or ""
    )


def _entry_to_candidate(entry: dict[str, Any]) -> Candidate | None:
    video_id = entry.get("id")
    if not video_id:
        return None

    url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
    title = entry.get("title") or ""
    channel = _entry_channel(entry)
    duration = entry.get("duration")
    duration_s = int(duration) if duration is not None else 0
    view_count = entry.get("view_count")
    views = int(view_count) if view_count is not None else 0
    description = entry.get("description") or ""

    return Candidate(
        video_id=str(video_id),
        url=str(url),
        title=str(title),
        channel=str(channel),
        duration_s=duration_s,
        view_count=views,
        is_art_track=_is_art_track(str(channel), str(description)),
        description=str(description),
    )


def _search_query(query: str, limit: int) -> list[dict[str, Any]]:
    with yt_dlp.YoutubeDL(_FLAT_OPTS) as ydl:
        result = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    if not result:
        return []
    entries = result.get("entries") or []
    return [entry for entry in entries if entry is not None]


def _enrich_candidate(flat: Candidate) -> Candidate:
    """Fetch full metadata when flat search omits description or duration."""
    if flat.description and flat.duration_s > 0:
        return flat

    with yt_dlp.YoutubeDL(_DETAIL_OPTS) as ydl:
        try:
            info = ydl.extract_info(flat.url, download=False)
        except yt_dlp.utils.DownloadError:
            return flat

    if not info:
        return flat

    enriched = _entry_to_candidate(info)
    return enriched if enriched is not None else flat


def _query_for_track(track: Track, *, with_audio: bool = False) -> str:
    base = f"{' '.join(track.artists)} {track.name}".strip()
    if with_audio:
        return f"{base} audio"
    return base


def search_candidates(track: Track, limit: int = 10) -> list[Candidate]:
    """Search YouTube for likely audio matches to a Spotify track."""
    seen: set[str] = set()
    candidates: list[Candidate] = []

    def add_from_query(with_audio: bool) -> None:
        for entry in _search_query(_query_for_track(track, with_audio=with_audio), limit):
            video_id = entry.get("id")
            if not video_id or video_id in seen:
                continue
            seen.add(str(video_id))
            flat = _entry_to_candidate(entry)
            if flat is None:
                continue
            candidates.append(_enrich_candidate(flat))

    add_from_query(with_audio=False)
    if len(candidates) < 3:
        add_from_query(with_audio=True)

    return candidates
