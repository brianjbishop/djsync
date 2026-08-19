"""Write and read ID3 tags for downloaded tracks."""

from __future__ import annotations

from pathlib import Path

from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPOS, TRCK, TXXX, ID3NoHeaderError

from djsync.matcher.candidate import Candidate
from djsync.models import Track


def write_tags(path: Path, track: Track, cand: Candidate) -> None:
    """Write standard and djsync-specific ID3 tags to *path*."""
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TALB")
    tags.add(TIT2(encoding=3, text=track.name))
    tags.add(TPE1(encoding=3, text=", ".join(track.artists)))
    tags.add(TALB(encoding=3, text=track.album))
    if track.track_number > 0:
        tags.add(TRCK(encoding=3, text=str(track.track_number)))
    if track.disc_number > 1:
        tags.add(TPOS(encoding=3, text=str(track.disc_number)))
    tags.add(TXXX(encoding=3, desc="SPOTIFY_ID", text=track.id))
    tags.add(TXXX(encoding=3, desc="DJSYNC_SOURCE", text=cand.url))
    tags.save(path)


def read_spotify_id(path: Path) -> str | None:
    """Return the SPOTIFY_ID TXXX frame value, if present."""
    try:
        tags = ID3(path)
    except (ID3NoHeaderError, OSError):
        return None

    for frame in tags.getall("TXXX"):
        if frame.desc == "SPOTIFY_ID" and frame.text:
            return str(frame.text[0])
    return None
