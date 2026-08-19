"""Sync a single playlist: match, download, and tag tracks."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import spotipy

from djsync import config, spotify
from djsync.config import FIXTURES_DIR, PLAYLISTS_DIR, WEIGHTS
from djsync.download import DownloadError, download, sanitize_filename
from djsync.fixtures import record_match
from djsync.library import existing_spotify_ids, playlist_folder
from djsync.matcher.candidate import Candidate
from djsync.matcher import search, score
from djsync.tagging import write_tags


@dataclass
class SyncResult:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


LogCallback = Callable[[str], None]
TrackCallback = Callable[[str], None]
ProgressCallback = Callable[[], None]


def sync_playlist(
    client: spotipy.Spotify,
    playlist: spotify.Playlist,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    on_log: LogCallback | None = None,
    on_track: TrackCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> SyncResult:
    """Match, download, and tag missing tracks for one playlist."""
    result = SyncResult()

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    tracks = spotify.fetch_tracks(client, playlist.id)
    folder = playlist_folder(playlist.name, PLAYLISTS_DIR)
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
            record_match(track, candidates, chosen, ranked, FIXTURES_DIR)
            if on_progress:
                on_progress()

    return result
