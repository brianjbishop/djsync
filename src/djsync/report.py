"""Daily progress report from cache and local ledgers."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from djsync import agent, cache, downloads, events, quota
from djsync.config import DAILY_REQUEST_BUDGET, Destination, get_destination

_WINDOW_24H = timedelta(hours=24)
_WINDOW_7D = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _progress_row(
    label: str,
    downloaded: int,
    total: int,
    *,
    empty_label: str | None = None,
) -> str:
    if total == 0:
        status = empty_label or "none"
        return f"  {label:<12} {status}"
    pct = 100.0 * downloaded / total
    return f"  {label:<12} {downloaded:>6,} / {total:<6,}  ({pct:5.1f}%)"


def playlist_progress(data: dict[str, Any] | None) -> tuple[int, int]:
    if not data:
        return 0, 0
    have = 0
    total = 0
    for entry in data.get("playlists") or []:
        if "d" not in (entry.get("sigils") or []):
            continue
        count = int(entry.get("track_count") or 0)
        downloaded = int(entry.get("downloaded_count") or 0)
        total += count
        have += min(downloaded, count)
    return have, total


def album_progress(data: dict[str, Any] | None) -> tuple[int, int]:
    if not data:
        return 0, 0
    have = 0
    total = 0
    for entry in data.get("albums") or []:
        count = int(entry.get("total_tracks") or 0)
        downloaded = int(entry.get("downloaded_count") or 0)
        total += count
        have += min(downloaded, count)
    return have, total


def _download_timestamps(*, now: datetime | None = None) -> list[datetime]:
    now = now or _now()
    cutoff = now - _WINDOW_7D
    path = downloads.LEDGER_PATH
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    result: list[datetime] = []
    for raw in data.get("downloads") or []:
        if not isinstance(raw, str):
            continue
        ts = _parse_ts(raw)
        if ts is not None and ts >= cutoff:
            result.append(ts)
    return sorted(result)


def count_downloads_since(hours: float, *, now: datetime | None = None) -> int:
    now = now or _now()
    cutoff = now - timedelta(hours=hours)
    return sum(1 for ts in _download_timestamps(now=now) if ts >= cutoff)


def projected_completion_date(
    data: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Estimate finish date from trailing 7-day observed download rate."""
    now = now or _now()
    if not data:
        return None
    have, total = agent.library_progress(data)
    remaining = max(0, total - have)
    if remaining == 0:
        return now

    cutoff = now - _WINDOW_7D
    recent = [ts for ts in _download_timestamps(now=now) if ts >= cutoff]
    if not recent:
        return None

    days = max((now - recent[0]).total_seconds() / 86400.0, 1.0)
    rate = len(recent) / days
    if rate <= 0:
        return None
    days_left = remaining / rate
    return now + timedelta(days=days_left)


def _format_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _format_local_dt(dt: datetime) -> str:
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M %Z")


def build_report(
    *,
    data: dict[str, Any] | None = None,
    dest: Destination | None = None,
    now: datetime | None = None,
) -> str:
    now = now or _now()
    dest = dest or get_destination()
    data = data if data is not None else cache.load_cache()
    agent_state = agent.load_state()
    recent = events.events_since(24, now=now)

    tracks_24h = count_downloads_since(24, now=now)
    completions_24h = [e for e in recent if e.get("kind") == "completion"]
    failures_24h = [e for e in recent if e.get("kind") == "failure"]
    unverified = [e for e in recent if e.get("kind") == "unverified_explicit"]

    lockout = quota.get_lockout(now=now)
    quota_used = quota.used_last_24h(now=now)
    drive_mounted = dest.mounted
    free_bytes = dest.free_bytes()

    has_problem = (
        not drive_mounted
        or lockout is not None
        or bool(failures_24h)
        or agent_state.get("stop_reason") == "circuit_breaker"
    )
    has_activity = tracks_24h > 0 or completions_24h or unverified

    if not has_activity and not has_problem:
        return "djsync: no activity in the last 24 hours."

    lines: list[str] = ["djsync daily report", ""]

    pl_have, pl_total = playlist_progress(data)
    al_have, al_total = album_progress(data)
    lines.append("Progress")
    lines.append(_progress_row("Playlists", pl_have, pl_total))
    lines.append(
        _progress_row("Albums", al_have, al_total, empty_label="not scanned yet")
    )
    lines.append("")

    lines.append("Last 24 hours")
    lines.append(f"  Tracks downloaded: {tracks_24h}")
    playlist_done = sum(1 for e in completions_24h if e.get("collection") == "playlist")
    album_done = sum(1 for e in completions_24h if e.get("collection") == "album")
    lines.append(f"  Playlists completed: {playlist_done}")
    lines.append(f"  Albums completed: {album_done}")
    lines.append("")

    projected = projected_completion_date(data, now=now)
    lines.append("Projected completion")
    if projected is None:
        have, total = agent.library_progress(data)
        if have >= total and total > 0:
            lines.append("  Complete")
        else:
            lines.append("  Unknown (no downloads in the last 7 days)")
    else:
        lines.append(f"  {_format_local_dt(projected)} (7-day average rate)")
    lines.append("")

    lines.append("Failures (last 24h)")
    if failures_24h:
        for entry in failures_24h:
            reason = str(entry.get("reason") or "unknown")
            tripped = "yes" if entry.get("circuit_breaker") else "no"
            ts = _parse_ts(str(entry.get("ts") or ""))
            when = _format_local_dt(ts) if ts else "?"
            lines.append(f"  [{when}] {reason} (circuit breaker: {tripped})")
    elif agent_state.get("stop_reason") == "circuit_breaker":
        lines.append(
            f"  Circuit breaker tripped: {agent_state.get('last_error') or 'unknown'}"
        )
    else:
        lines.append("  None")
    lines.append("")

    lines.append("Spotify quota")
    lines.append(f"  Used today: {quota_used} / {DAILY_REQUEST_BUDGET}")
    if lockout:
        reset_at = _parse_ts(str(lockout.get("reset_at") or ""))
        reset_text = _format_local_dt(reset_at) if reset_at else "unknown"
        reason = lockout.get("reason") or "lockout"
        lines.append(f"  Lockout: {reason} (resets {reset_text})")
    else:
        lines.append("  Lockout: none")
    lines.append("")

    lines.append("Explicit review (Spotify explicit, match had no explicit marker)")
    if unverified:
        for entry in unverified:
            artists = ", ".join(entry.get("artists") or [])
            name = entry.get("name") or "?"
            chosen = entry.get("chosen_title") or "?"
            lines.append(f"  {artists} — {name}")
            lines.append(f"       -> {chosen}")
    else:
        lines.append("  None in the last 24 hours")
    lines.append("")

    lines.append("Drive")
    lines.append(f"  Mounted: {'yes' if drive_mounted else 'no'}")
    lines.append(f"  Free space: {_format_bytes(free_bytes)}")
    lines.append(f"  Daily download cap: {downloads.effective_daily_cap()}/24h")

    return "\n".join(lines)


def report_embed_color(
    *,
    data: dict[str, Any] | None = None,
    dest: Destination | None = None,
    now: datetime | None = None,
) -> int:
    """Pick embed colour: green normal, amber attention, red blocked."""
    from djsync import beeper

    now = now or _now()
    dest = dest or get_destination()
    data = data if data is not None else cache.load_cache()
    agent_state = agent.load_state()
    recent = events.events_since(24, now=now)
    failures_24h = [e for e in recent if e.get("kind") == "failure"]
    lockout = quota.get_lockout(now=now)
    drive_mounted = dest.mounted

    if lockout is not None or (
        not drive_mounted and beeper.drive_unmounted_over_24h(agent_state, now=now)
    ):
        return beeper.COLOR_RED
    if failures_24h or agent_state.get("stop_reason") == "circuit_breaker":
        return beeper.COLOR_AMBER
    return beeper.COLOR_GREEN


def send_email(
    subject: str,
    body: str,
    recipient: str,
    *,
    run_osascript: Any | None = None,
) -> None:
    """Send via Mail.app AppleScript. *run_osascript* is injectable for tests."""
    runner = run_osascript or subprocess.run
    escaped_subject = subject.replace("\\", "\\\\").replace('"', '\\"')
    escaped_body = body.replace("\\", "\\\\").replace('"', '\\"')
    escaped_recipient = recipient.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Mail"\n'
        f'   set m to make new outgoing message with properties '
        f'{{subject:"{escaped_subject}", content:"{escaped_body}", visible:false}}\n'
        f'   tell m to make new to recipient at end of to recipients '
        f'with properties {{address:"{escaped_recipient}"}}\n'
        "   send m\n"
        "end tell"
    )
    runner(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
        timeout=30,
    )


def run_report(
    *,
    email: bool = False,
    beeper_post: bool = False,
    recipient: str | None = None,
    now: datetime | None = None,
    dest: Destination | None = None,
    run_osascript: Any | None = None,
    urlopen: Any | None = None,
) -> int:
    """Build and print, email, or Beeper-post the daily report. Never raises."""
    from djsync import beeper
    from djsync.config import DJSYNC_REPORT_EMAIL, get_beeper_settings

    now = now or _now()
    dest = dest or get_destination()
    text = build_report(now=now, dest=dest)

    if beeper_post:
        _, _, token = get_beeper_settings()
        if not token:
            print(beeper.missing_token_message(), flush=True)
            return 1
        title = f"djsync daily report — {now.astimezone().strftime('%Y-%m-%d')}"
        body = text
        if body.startswith("djsync daily report"):
            body = body.split("\n", 1)[-1].lstrip("\n")
        ok = beeper.send_report(title=title, body=body, now=now, urlopen=urlopen)
        return 0 if ok else 1

    if email:
        to = recipient or DJSYNC_REPORT_EMAIL
        subject = f"djsync report — {now.astimezone().strftime('%Y-%m-%d')}"
        try:
            send_email(subject, text, to, run_osascript=run_osascript)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Failed to send email: {exc}", flush=True)
            return 1
        return 0

    print(text)
    return 0
