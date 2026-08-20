"""Pure helpers for the web UI (no I/O or network)."""

from __future__ import annotations

from typing import Any, Literal

DownloadStatus = Literal["none", "partial", "complete", "not_scanned"]
SortKey = Literal["name", "track_count", "last_added", "status"]
AlbumSortKey = Literal[
    "name", "artist", "total_tracks", "status", "added_at", "release_date"
]
GroupBy = Literal["none", "sigil", "genre"]
AlbumGroupBy = Literal["none", "artist"]

STATUS_ORDER = {"not_scanned": -1, "none": 0, "partial": 1, "complete": 2}
SIGIL_ANY = "__any__"
SIGIL_NONE = "__none__"
BYTES_PER_TRACK = int(9.5 * 1024 * 1024)


def compute_status(downloaded_count: int | None, track_count: int) -> DownloadStatus:
    """Return download status from local vs playlist track counts."""
    if downloaded_count is None:
        return "not_scanned"
    if track_count == 0 or downloaded_count >= track_count:
        return "complete"
    if downloaded_count == 0:
        return "none"
    return "partial"


def playlist_matches_sigil_filter(
    playlist: dict[str, Any],
    sigil_filters: set[str],
) -> bool:
    """Return True if *playlist* matches the sigil filter set."""
    if not sigil_filters:
        return True
    sigils = set(playlist.get("sigils") or [])
    matches = False
    if SIGIL_NONE in sigil_filters and not sigils:
        matches = True
    if SIGIL_ANY in sigil_filters and sigils:
        matches = True
    if sigils & (sigil_filters - {SIGIL_ANY, SIGIL_NONE}):
        matches = True
    return matches


def filter_playlists(
    playlists: list[dict[str, Any]],
    *,
    search: str = "",
    sigil_filters: set[str] | None = None,
    status_filters: set[str] | None = None,
    added_since: str | None = None,
) -> list[dict[str, Any]]:
    """Filter playlists; conditions combine with AND across filter types."""
    search_lower = search.strip().casefold()
    sigil_filters = sigil_filters or set()
    status_filters = status_filters or set()

    filtered: list[dict[str, Any]] = []
    for playlist in playlists:
        if search_lower and search_lower not in playlist.get("name", "").casefold():
            continue
        if sigil_filters and not playlist_matches_sigil_filter(playlist, sigil_filters):
            continue
        if status_filters and playlist.get("status", "none") not in status_filters:
            continue
        if added_since:
            last_added = playlist.get("last_added")
            if not last_added or last_added < added_since:
                continue
        filtered.append(playlist)
    return filtered


def collect_sigil_filter_options(playlists: list[dict[str, Any]]) -> list[str]:
    """Return sorted unique sigils present in *playlists*."""
    sigils: set[str] = set()
    for playlist in playlists:
        sigils.update(playlist.get("sigils") or [])
    return sorted(sigils)


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


def sort_albums(
    albums: list[dict[str, Any]],
    key: AlbumSortKey,
    reverse: bool = False,
) -> list[dict[str, Any]]:
    """Return albums sorted by *key*."""
    if key == "name":
        return sorted(albums, key=lambda a: a["name"].lower(), reverse=reverse)
    if key == "artist":
        return sorted(
            albums,
            key=lambda a: ", ".join(a.get("artists") or []).lower(),
            reverse=reverse,
        )
    if key == "total_tracks":
        return sorted(albums, key=lambda a: a["total_tracks"], reverse=reverse)
    if key == "added_at":
        return sorted(
            albums,
            key=lambda a: a.get("added_at") or "",
            reverse=reverse,
        )
    if key == "release_date":
        return sorted(
            albums,
            key=lambda a: a.get("release_date") or "",
            reverse=reverse,
        )
    if key == "status":
        return sorted(
            albums,
            key=lambda a: STATUS_ORDER.get(a.get("status", "none"), 0),
            reverse=reverse,
        )
    return list(albums)


def group_albums(
    albums: list[dict[str, Any]],
    group_by: AlbumGroupBy,
) -> list[dict[str, Any]]:
    """Return grouped rows for album rendering."""
    if group_by == "none":
        return [{"group": None, "album": a} for a in albums]

    groups: dict[str, list[dict[str, Any]]] = {}
    for album in albums:
        artists = album.get("artists") or []
        if not artists:
            groups.setdefault("(unknown artist)", []).append(album)
        else:
            for artist in sorted(artists):
                groups.setdefault(artist, []).append(album)
    rows: list[dict[str, Any]] = []
    for label in sorted(groups, key=str.lower):
        rows.append({"group": label, "album": None})
        for album in groups[label]:
            rows.append({"group": None, "album": album})
    return rows


def selection_summary(
    playlists: list[dict[str, Any]],
    selected_ids: set[str],
) -> dict[str, Any]:
    """Summarize selected playlists for the summary bar and confirm dialog."""
    selected = [p for p in playlists if p["id"] in selected_ids]
    total_tracks = sum(p["track_count"] for p in selected)
    missing_tracks = sum(
        max(0, p["track_count"] - (p.get("downloaded_count") or 0))
        for p in selected
        if p.get("status") != "not_scanned"
    )
    unscanned = sum(1 for p in selected if p.get("status") == "not_scanned")
    size_bytes = estimate_size_bytes(missing_tracks)
    return {
        "playlist_count": len(selected),
        "total_tracks": total_tracks,
        "missing_tracks": missing_tracks,
        "unscanned_count": unscanned,
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


def album_selection_summary(
    albums: list[dict[str, Any]],
    selected_ids: set[str],
) -> dict[str, Any]:
    """Summarize selected albums for the summary bar and confirm dialog."""
    selected = [a for a in albums if a["id"] in selected_ids]
    total_tracks = sum(a["total_tracks"] for a in selected)
    missing_tracks = sum(
        max(0, a["total_tracks"] - a.get("downloaded_count", 0)) for a in selected
    )
    size_bytes = estimate_size_bytes(missing_tracks)
    return {
        "album_count": len(selected),
        "total_tracks": total_tracks,
        "missing_tracks": missing_tracks,
        "size_bytes": size_bytes,
        "size_display": format_size(size_bytes),
        "albums": [
            {
                "id": a["id"],
                "name": a["name"],
                "artists": a.get("artists") or [],
                "total_tracks": a["total_tracks"],
                "downloaded_count": a.get("downloaded_count", 0),
                "missing": max(0, a["total_tracks"] - a.get("downloaded_count", 0)),
            }
            for a in selected
        ],
    }
