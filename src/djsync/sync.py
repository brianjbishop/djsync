"""Sync a single playlist: match, download, and tag tracks."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import spotipy

from djsync.models import Track
from djsync import cache, config, spotify
from djsync.config import WEIGHTS, fixtures_dir, get_destination
from djsync.download import DownloadError, download, sanitize_filename
from djsync.fixtures import record_match
from djsync.library import (
    album_folder,
    album_track_filename,
    existing_spotify_ids,
    playlist_folder,
)
from djsync.matcher.candidate import Candidate
from djsync.matcher import search, score
from djsync.matcher.score import content_marker
from djsync.tagging import write_tags


@dataclass
class SyncResult:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    unverified_explicit: list[dict[str, str | list[str]]] = field(default_factory=list)


def _unverified_explicit_entry(
    track: spotify.Track,
    chosen: Candidate,
) -> dict[str, str | list[str]]:
    return {
        "track_id": track.id,
        "name": track.name,
        "artists": list(track.artists),
        "chosen_title": chosen.title,
        "chosen_url": chosen.url,
    }


def _note_unverified_explicit(
    result: SyncResult,
    track: spotify.Track,
    chosen: Candidate | None,
) -> None:
    if not track.explicit or chosen is None:
        return
    if content_marker(chosen) == "explicit":
        return
    result.unverified_explicit.append(_unverified_explicit_entry(track, chosen))


LogCallback = Callable[[str], None]
TrackCallback = Callable[[str], None]
ProgressCallback = Callable[[], None]


def sync_playlist(
    client: spotipy.Spotify,
    playlist: spotify.Playlist,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    tracks: list[Track] | None = None,
    refresh: bool = False,
    cached: dict[str, Any] | None = None,
    on_log: LogCallback | None = None,
    on_track: TrackCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> SyncResult:
    """Match, download, and tag missing tracks for one playlist."""
    result = SyncResult()

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    if tracks is None:
        tracks = cache.resolve_playlist_for_sync(
            client,
            playlist.name,
            refresh=refresh,
            cached=cached,
        )[1]
    dest = get_destination()
    folder = playlist_folder(playlist.name, dest.path)
    if dest.mounted:
        folder.mkdir(parents=True, exist_ok=True)

    local_ids = existing_spotify_ids(folder)
    spotify_ids = {t.id for t in tracks}
    removed = local_ids - spotify_ids
    for track_id in sorted(removed):
        log(f"removed (not deleted): spotify_id={track_id}")

    all_missing = [t for t in tracks if t.id not in local_ids]
    missing = all_missing[:limit] if limit is not None else all_missing
    result.skipped = len(tracks) - len(all_missing)

    for track in missing:
        if on_track:
            on_track(track.name)
        candidates: list[Candidate] = []
        ranked: list[tuple[Candidate, float, dict[str, float]]] = []
        chosen: Candidate | None = None
        try:
            candidates = search.search_candidates(track)
            ranked = score.rank(track, candidates, WEIGHTS)
            chosen = ranked[0][0] if ranked else None

            if chosen is None:
                log(f"FAIL  {track.name} — no candidates found")
                result.failed += 1
                continue

            _note_unverified_explicit(result, track, chosen)

            delta_s = abs(chosen.duration_s - track.duration_ms / 1000)
            total = ranked[0][1]
            prefix = "DRY-RUN" if dry_run else "PICK"
            log(
                f"{prefix}  {track.name}\n"
                f"       -> {chosen.title} | {chosen.channel} | "
                f"Δ{delta_s:.0f}s | score={total:.2f}"
            )

            if dry_run:
                continue

            dest = folder / f"{sanitize_filename(track.name)}.mp3"
            path = download(chosen, dest)
            write_tags(path, track, chosen)
            result.downloaded += 1
            if result.downloaded < len(missing):
                lo, hi = config.SLEEP_BETWEEN_DOWNLOADS
                time.sleep(random.uniform(lo, hi))
        except DownloadError as exc:
            log(f"FAIL  {track.name} — {exc}")
            result.failed += 1
        except Exception as exc:
            log(f"FAIL  {track.name} — {exc}")
            result.failed += 1
        finally:
            record_match(track, candidates, chosen, ranked, fixtures_dir())
            if on_progress:
                on_progress()

    return result


def sync_album(
    client: spotipy.Spotify,
    album: spotify.Album,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    tracks: list[Track] | None = None,
    refresh: bool = False,
    cached: dict[str, Any] | None = None,
    on_log: LogCallback | None = None,
    on_track: TrackCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> SyncResult:
    """Match, download, and tag missing tracks for one saved album."""
    result = SyncResult()

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    if tracks is None:
        tracks = cache.resolve_album_for_sync(
            client,
            album,
            refresh=refresh,
            cached=cached,
        )
    dest = get_destination()
    folder = album_folder(album.artists, album.name, dest.path_for("albums"))
    if dest.mounted:
        folder.mkdir(parents=True, exist_ok=True)

    multi_disc = any(t.disc_number > 1 for t in tracks)

    local_ids = existing_spotify_ids(folder)
    spotify_ids = {t.id for t in tracks}
    removed = local_ids - spotify_ids
    for track_id in sorted(removed):
        log(f"removed (not deleted): spotify_id={track_id}")

    all_missing = [t for t in tracks if t.id not in local_ids]
    missing = all_missing[:limit] if limit is not None else all_missing
    result.skipped = len(tracks) - len(all_missing)

    for track in missing:
        if on_track:
            on_track(track.name)
        candidates: list[Candidate] = []
        ranked: list[tuple[Candidate, float, dict[str, float]]] = []
        chosen: Candidate | None = None
        try:
            candidates = search.search_candidates(track, album_search=True)
            ranked = score.rank(track, candidates, WEIGHTS)
            chosen = ranked[0][0] if ranked else None

            if chosen is None:
                log(f"FAIL  {track.name} — no candidates found")
                result.failed += 1
                continue

            _note_unverified_explicit(result, track, chosen)

            delta_s = abs(chosen.duration_s - track.duration_ms / 1000)
            total = ranked[0][1]
            prefix = "DRY-RUN" if dry_run else "PICK"
            log(
                f"{prefix}  {track.name}\n"
                f"       -> {chosen.title} | {chosen.channel} | "
                f"Δ{delta_s:.0f}s | score={total:.2f}"
            )

            if dry_run:
                continue

            filename = album_track_filename(track, multi_disc=multi_disc)
            path = download(chosen, folder / filename)
            write_tags(path, track, chosen)
            result.downloaded += 1
            if result.downloaded < len(missing):
                lo, hi = config.SLEEP_BETWEEN_DOWNLOADS
                time.sleep(random.uniform(lo, hi))
        except DownloadError as exc:
            log(f"FAIL  {track.name} — {exc}")
            result.failed += 1
        except Exception as exc:
            log(f"FAIL  {track.name} — {exc}")
            result.failed += 1
        finally:
            record_match(track, candidates, chosen, ranked, fixtures_dir())
            if on_progress:
                on_progress()

    return result
