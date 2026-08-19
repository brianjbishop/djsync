"""Tests for candidate scoring (pure, no network)."""

from __future__ import annotations

from djsync.config import WEIGHTS
from djsync.matcher.candidate import Candidate
from djsync.matcher.score import rank, score_candidate
from djsync.models import Track


def _track(
    name: str = "Blinding Lights",
    artists: tuple[str, ...] = ("The Weeknd",),
    duration_ms: int = 200_000,
    *,
    explicit: bool = False,
) -> Track:
    return Track(
        id="t1",
        name=name,
        artists=artists,
        album="After Hours",
        duration_ms=duration_ms,
        isrc=None,
        explicit=explicit,
    )


def _cand(
    *,
    title: str,
    channel: str = "ArtistVEVO",
    duration_s: int = 200,
    view_count: int = 1_000,
    is_art_track: bool = False,
    video_id: str | None = None,
    description: str = "",
) -> Candidate:
    vid = video_id or title.replace(" ", "_").lower()
    return Candidate(
        video_id=vid,
        url=f"https://www.youtube.com/watch?v={vid}",
        title=title,
        channel=channel,
        duration_s=duration_s,
        view_count=view_count,
        is_art_track=is_art_track,
        description="",
    )


def test_art_track_beats_sped_up_high_view() -> None:
    track = _track(duration_ms=200_000)
    art = _cand(
        title="Blinding Lights",
        channel="The Weeknd - Topic",
        duration_s=200,
        view_count=50_000,
        is_art_track=True,
        video_id="art",
    )
    sped = _cand(
        title="Blinding Lights sped up",
        channel="TikTok Edits",
        duration_s=200,
        view_count=50_000_000,
        video_id="sped",
    )

    ranked = rank(track, [sped, art], WEIGHTS)
    assert ranked[0][0].video_id == "art"


def test_live_version_loses_to_studio() -> None:
    track = _track(name="Starlight", duration_ms=180_000)
    studio = _cand(
        title="Starlight",
        channel="Muse - Topic",
        duration_s=180,
        is_art_track=True,
        video_id="studio",
    )
    live = _cand(
        title="Starlight (Live at Wembley)",
        channel="Muse",
        duration_s=300,
        video_id="live",
    )

    ranked = rank(track, [live, studio], WEIGHTS)
    assert ranked[0][0].video_id == "studio"


def test_remix_not_penalized_when_spotify_title_has_remix() -> None:
    track = _track(name="Midnight City (Eric Prydz Remix)", duration_ms=420_000)
    remix = _cand(
        title="Midnight City Eric Prydz Remix",
        channel="M83 - Topic",
        duration_s=420,
        is_art_track=True,
        video_id="remix",
    )
    original = _cand(
        title="Midnight City",
        channel="M83 - Topic",
        duration_s=244,
        is_art_track=True,
        video_id="orig",
    )

    remix_score, remix_breakdown = score_candidate(track, remix, WEIGHTS)
    assert remix_breakdown["negative_flags"] == 0.0
    assert remix_score > score_candidate(track, original, WEIGHTS)[0]


def test_explicit_marked_beats_unmarked_same_duration() -> None:
    track = _track(name="Pick Up the Phone", duration_ms=253_000, explicit=True)
    unmarked = _cand(
        title="pick up the phone",
        channel="Young Thug",
        duration_s=253,
        video_id="clean",
    )
    marked = _cand(
        title="Pick Up the Phone (Audio) [Explicit]",
        channel="Cactus Jack",
        duration_s=253,
        video_id="explicit",
    )

    ranked = rank(track, [unmarked, marked], WEIGHTS)
    assert ranked[0][0].video_id == "explicit"


def test_clean_candidate_penalized_for_explicit_track() -> None:
    track = _track(name="Pick Up the Phone", duration_ms=253_000, explicit=True)
    clean = _cand(
        title="Pick Up the Phone (Clean)",
        channel="Label",
        duration_s=253,
        video_id="clean",
    )
    neutral = _cand(
        title="Pick Up the Phone",
        channel="Young Thug",
        duration_s=253,
        video_id="neutral",
    )

    ranked = rank(track, [clean, neutral], WEIGHTS)
    assert ranked[0][0].video_id == "neutral"


def test_explicit_marker_does_not_override_duration() -> None:
    track = _track(name="Pick Up the Phone", duration_ms=253_000, explicit=True)
    wrong_duration_explicit = _cand(
        title="Pick Up the Phone (Explicit)",
        channel="Travis Scott",
        duration_s=303,
        video_id="wrong",
    )
    correct_unmarked = _cand(
        title="pick up the phone",
        channel="Young Thug",
        duration_s=253,
        video_id="right",
    )

    ranked = rank(track, [wrong_duration_explicit, correct_unmarked], WEIGHTS)
    assert ranked[0][0].video_id == "right"


def test_non_explicit_track_not_skewed_toward_explicit_uploads() -> None:
    track = _track(name="Blinding Lights", duration_ms=200_000, explicit=False)
    explicit_upload = _cand(
        title="Blinding Lights [Explicit]",
        channel="Fan Channel",
        duration_s=200,
        video_id="explicit",
    )
    neutral = _cand(
        title="Blinding Lights",
        channel="The Weeknd",
        duration_s=200,
        video_id="neutral",
    )

    ranked = rank(track, [explicit_upload, neutral], WEIGHTS)
    assert ranked[0][0].video_id == "neutral"
    assert score_candidate(track, explicit_upload, WEIGHTS)[1]["clean_mismatch"] < 0
