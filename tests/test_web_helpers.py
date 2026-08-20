"""Tests for pure web UI helpers."""

from __future__ import annotations

from djsync.web_helpers import (
    BYTES_PER_TRACK,
    SIGIL_ANY,
    SIGIL_NONE,
    compute_status,
    estimate_size_bytes,
    filter_playlists,
    format_size,
    group_playlists,
    sort_playlists,
)


def _playlist(
    *,
    id_: str = "p1",
    name: str = "Alpha",
    track_count: int = 10,
    downloaded_count: int | None = 0,
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


def test_compute_status_not_scanned() -> None:
    assert compute_status(None, 10) == "not_scanned"


def test_filter_playlists_combined_sigil_status_search() -> None:
    playlists = [
        _playlist(id_="a", name="Warmup Set", sigils=["d"], status="partial"),
        _playlist(id_="b", name="Chill Mix", sigils=["ave"], status="none"),
        _playlist(id_="c", name="Plain list", sigils=[], status="not_scanned", downloaded_count=None),
    ]
    filtered = filter_playlists(
        playlists,
        search="mix",
        sigil_filters={"ave"},
        status_filters={"none"},
    )
    assert [p["id"] for p in filtered] == ["b"]


def test_filter_playlists_sigil_any_and_none() -> None:
    playlists = [
        _playlist(id_="a", name="A", sigils=["d"]),
        _playlist(id_="b", name="B", sigils=[]),
    ]
    any_only = filter_playlists(playlists, sigil_filters={SIGIL_ANY})
    assert [p["id"] for p in any_only] == ["a"]
    none_only = filter_playlists(playlists, sigil_filters={SIGIL_NONE})
    assert [p["id"] for p in none_only] == ["b"]


def test_filter_select_all_scope_is_filtered_subset() -> None:
    playlists = [
        _playlist(id_="a", name="Alpha", sigils=["d"]),
        _playlist(id_="b", name="Beta", sigils=[]),
    ]
    filtered = filter_playlists(playlists, sigil_filters={"d"})
    selected_ids = {p["id"] for p in filtered}
    assert selected_ids == {"a"}


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


def test_album_status_computation() -> None:
    assert compute_status(0, 10) == "none"
    assert compute_status(4, 10) == "partial"
    assert compute_status(10, 10) == "complete"
    assert compute_status(0, 0) == "complete"


def test_sort_albums_by_artist() -> None:
    from djsync.web_helpers import sort_albums

    albums = [
        {"id": "a", "name": "Z Album", "artists": ["Zed"], "total_tracks": 1, "status": "none"},
        {"id": "b", "name": "A Album", "artists": ["Alpha"], "total_tracks": 1, "status": "none"},
    ]
    names = [a["name"] for a in sort_albums(albums, "artist")]
    assert names == ["A Album", "Z Album"]


def test_group_albums_by_artist() -> None:
    from djsync.web_helpers import group_albums

    albums = [
        {"id": "a", "name": "One", "artists": ["Shared"], "total_tracks": 1},
        {"id": "b", "name": "Two", "artists": ["Solo"], "total_tracks": 1},
    ]
    rows = group_albums(albums, "artist")
    headers = [r["group"] for r in rows if r["album"] is None]
    assert "Shared" in headers
    assert "Solo" in headers
