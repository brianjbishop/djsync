"""Tests for pure web UI helpers."""

from __future__ import annotations

from djsync.web_helpers import (
    BYTES_PER_TRACK,
    compute_status,
    estimate_size_bytes,
    format_size,
    group_playlists,
    sort_playlists,
)


def _playlist(
    *,
    id_: str = "p1",
    name: str = "Alpha",
    track_count: int = 10,
    downloaded_count: int = 0,
    status: str = "none",
    last_added: str | None = "2024-01-15",
    sigils: list[str] | None = None,
    genre: str | None = None,
) -> dict:
    return {
        "id": id_,
        "name": name,
        "track_count": track_count,
        "downloaded_count": downloaded_count,
        "status": status,
        "last_added": last_added,
        "sigils": sigils or [],
        "genre": genre,
    }


def test_compute_status_none() -> None:
    assert compute_status(0, 10) == "none"


def test_compute_status_partial() -> None:
    assert compute_status(3, 10) == "partial"


def test_compute_status_complete_when_all_downloaded() -> None:
    assert compute_status(10, 10) == "complete"


def test_compute_status_complete_when_empty_playlist() -> None:
    assert compute_status(0, 0) == "complete"


def test_estimate_size_bytes() -> None:
    assert estimate_size_bytes(2) == 2 * BYTES_PER_TRACK


def test_format_size_units() -> None:
    assert format_size(500) == "0.5 KB"
    assert format_size(1024 * 1024) == "1.0 MB"
    assert format_size(2 * 1024 * 1024 * 1024) == "2.00 GB"


def test_sort_playlists_by_name() -> None:
    playlists = [
        _playlist(id_="b", name="Bravo"),
        _playlist(id_="a", name="alpha"),
    ]
    sorted_names = [p["name"] for p in sort_playlists(playlists, "name")]
    assert sorted_names == ["alpha", "Bravo"]


def test_sort_playlists_by_track_count_desc() -> None:
    playlists = [
        _playlist(id_="a", track_count=5),
        _playlist(id_="b", track_count=20),
    ]
    counts = [p["track_count"] for p in sort_playlists(playlists, "track_count", reverse=True)]
    assert counts == [20, 5]


def test_sort_playlists_by_last_added() -> None:
    playlists = [
        _playlist(id_="a", last_added="2024-02-01"),
        _playlist(id_="b", last_added="2024-01-01"),
    ]
    dates = [p["last_added"] for p in sort_playlists(playlists, "last_added", reverse=True)]
    assert dates == ["2024-02-01", "2024-01-01"]


def test_sort_playlists_by_status() -> None:
    playlists = [
        _playlist(id_="a", status="complete"),
        _playlist(id_="b", status="none"),
        _playlist(id_="c", status="partial"),
    ]
    order = [p["status"] for p in sort_playlists(playlists, "status")]
    assert order == ["none", "partial", "complete"]


def test_group_playlists_none() -> None:
    playlists = [_playlist(id_="a"), _playlist(id_="b")]
    rows = group_playlists(playlists, "none")
    assert len(rows) == 2
    assert all(r["group"] is None and r["playlist"] is not None for r in rows)


def test_group_playlists_by_sigil() -> None:
    playlists = [
        _playlist(id_="a", name="A", sigils=["d", "warmup"]),
        _playlist(id_="b", name="B", sigils=[]),
    ]
    rows = group_playlists(playlists, "sigil")
    headers = [r["group"] for r in rows if r["playlist"] is None]
    assert "(no sigil)" in headers
    assert "$d" in headers
    assert "$warmup" in headers


def test_group_playlists_by_genre() -> None:
    playlists = [
        _playlist(id_="a", genre="House"),
        _playlist(id_="b", genre=None),
    ]
    rows = group_playlists(playlists, "genre")
    headers = [r["group"] for r in rows if r["playlist"] is None]
    assert "House" in headers
    assert "(unknown)" in headers
