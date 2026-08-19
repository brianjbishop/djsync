"""Pure helpers for the web UI (no I/O or network)."""

from __future__ import annotations

from typing import Any, Literal

DownloadStatus = Literal["none", "partial", "complete"]
SortKey = Literal["name", "track_count", "last_added", "status"]
GroupBy = Literal["none", "sigil", "genre"]

STATUS_ORDER = {"none": 0, "partial": 1, "complete": 2}
BYTES_PER_TRACK = int(9.5 * 1024 * 1024)


def compute_status(downloaded_count: int, track_count: int) -> DownloadStatus:
    """Return download status from local vs playlist track counts."""
    if track_count == 0 or downloaded_count >= track_count:
        return "complete"
    if downloaded_count == 0:
        return "none"
    return "partial"


def estimate_size_bytes(track_count: int) -> int:
    """Estimate download size at ~9.5 MB per track (320 kbps)."""
    return track_count * BYTES_PER_TRACK


def format_size(bytes_count: int) -> str:
    """Format a byte count for display."""
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} KB"
    if bytes_count < 1024 * 1024 * 1024:
        return f"{bytes_count / (1024 * 1024):.1f} MB"
    return f"{bytes_count / (1024 * 1024 * 1024):.2f} GB"


def sort_playlists(
    playlists: list[dict[str, Any]],
    key: SortKey,
    reverse: bool = False,
) -> list[dict[str, Any]]:
    """Return playlists sorted by *key*."""
    if key == "name":
        return sorted(playlists, key=lambda p: p["name"].lower(), reverse=reverse)
    if key == "track_count":
        return sorted(playlists, key=lambda p: p["track_count"], reverse=reverse)
    if key == "last_added":
        return sorted(
            playlists,
            key=lambda p: p.get("last_added") or "",
            reverse=reverse,
        )
    if key == "status":
        return sorted(
            playlists,
            key=lambda p: STATUS_ORDER.get(p.get("status", "none"), 0),
            reverse=reverse,
        )
    return list(playlists)


def group_playlists(
    playlists: list[dict[str, Any]],
    group_by: GroupBy,
) -> list[dict[str, Any]]:
    """Return grouped rows for rendering.

    Each returned dict has ``group`` (header label or None) and ``playlist``
    (the playlist dict, or None for header-only rows).
    """
    if group_by == "none":
        return [{"group": None, "playlist": p} for p in playlists]

    if group_by == "sigil":
        groups: dict[str, list[dict[str, Any]]] = {}
        for p in playlists:
            sigils = p.get("sigils") or []
            if not sigils:
                groups.setdefault("(no sigil)", []).append(p)
            else:
                for sigil in sorted(sigils):
                    groups.setdefault(f"${sigil}", []).append(p)
        rows: list[dict[str, Any]] = []
        for label in sorted(groups, key=str.lower):
            rows.append({"group": label, "playlist": None})
            for p in groups[label]:
                rows.append({"group": None, "playlist": p})
        return rows

    # group_by == "genre"
    groups = {}
    for p in playlists:
        genre = p.get("genre") or "(unknown)"
        groups.setdefault(genre, []).append(p)
    rows = []
    for label in sorted(groups, key=str.lower):
        rows.append({"group": label, "playlist": None})
        for p in groups[label]:
            rows.append({"group": None, "playlist": p})
    return rows


def selection_summary(
    playlists: list[dict[str, Any]],
    selected_ids: set[str],
) -> dict[str, Any]:
    """Summarize selected playlists for the summary bar and confirm dialog."""
    selected = [p for p in playlists if p["id"] in selected_ids]
    total_tracks = sum(p["track_count"] for p in selected)
    missing_tracks = sum(
        max(0, p["track_count"] - p.get("downloaded_count", 0)) for p in selected
    )
    size_bytes = estimate_size_bytes(missing_tracks)
    return {
        "playlist_count": len(selected),
        "total_tracks": total_tracks,
        "missing_tracks": missing_tracks,
        "size_bytes": size_bytes,
        "size_display": format_size(size_bytes),
        "playlists": [
            {
                "id": p["id"],
                "name": p["name"],
                "track_count": p["track_count"],
                "downloaded_count": p.get("downloaded_count", 0),
                "missing": max(0, p["track_count"] - p.get("downloaded_count", 0)),
            }
            for p in selected
        ],
    }
