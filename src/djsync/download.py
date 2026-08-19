"""Download YouTube audio via yt-dlp and ffmpeg."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yt_dlp

from djsync import config
from djsync.matcher.candidate import Candidate


class DownloadError(RuntimeError):
    """Raised when yt-dlp fails to download or convert audio."""


def sanitize_filename(stem: str) -> str:
    """Remove characters that are illegal or unsafe in filenames."""
    return stem.replace("/", "-").replace("\0", "")


def download(
    cand: Candidate,
    dest: Path,
    fmt: str = "mp3",
    bitrate: str = "320",
) -> Path:
    """Download *cand* as MP3 to *dest* (final ``.mp3`` path)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem = sanitize_filename(dest.stem)
    final_path = dest.parent / f"{stem}.{fmt}"
    outtmpl = str(dest.parent / f"{stem}.%(ext)s")

    opts: dict[str, Any] = {
        **config.youtube_opts(),
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
                "preferredquality": bitrate,
            }
        ],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([cand.url])
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(f"Failed to download {cand.url}: {exc}") from exc

    if not final_path.exists():
        raise DownloadError(
            f"Download finished but expected file is missing: {final_path}"
        )

    return final_path
