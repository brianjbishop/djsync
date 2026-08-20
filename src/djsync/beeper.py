"""Deliver notifications and read commands via Beeper Desktop local API."""

from __future__ import annotations

import errno
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from djsync import agent
from djsync.config import get_beeper_settings

logger = logging.getLogger(__name__)

COLOR_GREEN = 0x1DB954
COLOR_AMBER = 0xF5A623
COLOR_RED = 0xE74C3C

MESSAGE_MAX = 4000
UrlopenFn = Callable[..., Any]

_CAP_RE = re.compile(r"^cap\s+(\d+)\s*$", re.IGNORECASE)
_SKIP_RE = re.compile(r"^skip\s+(.+)$", re.IGNORECASE)
_UNSKIP_RE = re.compile(r"^unskip\s+(.+)$", re.IGNORECASE)
_SYNC_RE = re.compile(r"^sync\s+(.+)$", re.IGNORECASE)

_MISSING_TOKEN_MESSAGE = (
    "Beeper token is not configured (DJSYNC_BEEPER_TOKEN is unset).\n\n"
    "Run: djsync beeper-auth\n\n"
    "That walks OAuth against your local Beeper Desktop and prints the line to "
    "add to .env. The token is a secret — never commit it."
)


def _is_connection_refused(exc: BaseException) -> bool:
    """True when the failure means nothing is listening, not a broader outage."""
    inner = getattr(exc, "reason", exc)
    return getattr(inner, "errno", None) == errno.ECONNREFUSED


def missing_token_message() -> str:
    return _MISSING_TOKEN_MESSAGE


def truncate_text(text: str, max_len: int, marker: str = "…") -> str:
    if len(text) <= max_len:
        return text
    if max_len <= len(marker):
        return marker[:max_len]
    return text[: max_len - len(marker)] + marker


def record_delivery_failure(error: str, *, now: datetime | None = None) -> None:
    """Persist a Beeper delivery failure in agent state. Never raises."""
    now = now or datetime.now(UTC)
    try:
        agent.save_state(
            {
                "last_beeper_error": error,
                "last_beeper_failure": now.isoformat(),
            },
            now=now,
        )
    except Exception:
        logger.debug("failed to record Beeper delivery failure", exc_info=True)


def beeper_reachable(*, urlopen: UrlopenFn | None = None) -> bool:
    """Return True when Beeper Desktop responds to GET /v1/info."""
    base_url, _, _ = get_beeper_settings()
    opener = urlopen or urllib.request.urlopen
    req = urllib.request.Request(f"{base_url.rstrip('/')}/v1/info", method="GET")
    try:
        with opener(req, timeout=5) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _extract_message_text(message: dict[str, Any]) -> str:
    for key in ("text", "body", "content"):
        value = message.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("text") or value.get("body")
            if isinstance(nested, str):
                return nested.strip()
    return ""


def _message_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "messages", "data"):
        raw = payload.get(key)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    return []


def _message_id_key(msg_id: str) -> tuple[int, str | int]:
    try:
        return (0, int(msg_id))
    except ValueError:
        return (1, msg_id)


def fetch_messages(
    *,
    limit: int = 50,
    urlopen: UrlopenFn | None = None,
) -> list[dict[str, Any]]:
    """Return recent chat messages (oldest first). Never raises."""
    base_url, chat_id, token = get_beeper_settings()
    if not token:
        return []
    opener = urlopen or urllib.request.urlopen
    url = f"{base_url.rstrip('/')}/v1/chats/{chat_id}/messages?limit={limit}"
    req = urllib.request.Request(url, headers=_auth_headers(token), method="GET")
    try:
        with opener(req, timeout=30) as resp:
            raw = resp.read().decode()
        payload = json.loads(raw) if raw else {}
        items = _message_items(payload)
        items.sort(key=lambda m: _message_id_key(str(m.get("id") or "")))
        return items
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        logger.error("Beeper fetch messages failed: %s", exc)
        record_delivery_failure(str(exc))
        return []


def send_message(
    text: str,
    *,
    urlopen: UrlopenFn | None = None,
) -> bool:
    """POST plain text to the configured Beeper chat. Returns True on 2xx. Never raises."""
    base_url, chat_id, token = get_beeper_settings()
    if not token:
        return False
    # No reachability precheck here. It costs an extra request and, worse, it
    # reports every failure as "not running" - masking the actual cause (DNS,
    # timeout, refused) that you would need to debug this.
    opener = urlopen or urllib.request.urlopen
    url = f"{base_url.rstrip('/')}/v1/chats/{chat_id}/messages"
    body = json.dumps({"text": truncate_text(text, MESSAGE_MAX)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=_auth_headers(token),
        method="POST",
    )
    try:
        with opener(req, timeout=30) as resp:
            status = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in (401, 403):
            logger.error("Beeper token rejected: HTTP %s", status)
            record_delivery_failure(f"HTTP {status} (invalid token)")
            return False
        logger.error("Beeper send failed: HTTP %s", status)
        record_delivery_failure(f"HTTP {status}")
        return False
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if _is_connection_refused(exc):
            logger.warning("Beeper Desktop is not running (connection refused)")
            record_delivery_failure("Beeper Desktop not running")
        else:
            logger.error("Beeper network error: %s", exc)
            record_delivery_failure(str(exc))
        return False

    if 200 <= status < 300:
        return True
    logger.error("Beeper send failed: HTTP %s", status)
    record_delivery_failure(f"HTTP {status}")
    return False


def wrap_report_body(body: str) -> str:
    """Wrap the aligned progress table in a fenced code block for Discord markdown."""
    lines = body.splitlines()
    if not lines:
        return f"```\n{body}\n```"
    out: list[str] = []
    in_progress = False
    progress_lines: list[str] = []
    for line in lines:
        if line.strip() == "Progress":
            in_progress = True
            progress_lines = [line]
            continue
        if in_progress:
            if line.startswith("  ") and progress_lines:
                progress_lines.append(line)
                continue
            if progress_lines:
                out.append("```")
                out.extend(progress_lines)
                out.append("```")
                progress_lines = []
            in_progress = False
        out.append(line)
    if progress_lines:
        out.append("```")
        out.extend(progress_lines)
        out.append("```")
    return "\n".join(out)


def send_report(
    *,
    title: str,
    body: str,
    now: datetime | None = None,
    urlopen: UrlopenFn | None = None,
) -> bool:
    """Post a daily report to Beeper."""
    _ = now
    formatted = wrap_report_body(body)
    text = f"**{truncate_text(title, 256)}**\n\n{formatted}"
    return send_message(text, urlopen=urlopen)


def send_alert(
    *,
    title: str,
    body: str,
    now: datetime | None = None,
    urlopen: UrlopenFn | None = None,
) -> bool:
    _ = now
    text = f"**{truncate_text(title, 256)}**\n{body}"
    return send_message(text, urlopen=urlopen)


def _load_announced(state: dict[str, Any]) -> set[str]:
    raw = state.get("beeper_announced") or state.get("discord_announced")
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return set()


def _save_announced(announced: set[str], *, now: datetime | None = None) -> None:
    agent.save_state({"beeper_announced": sorted(announced)}, now=now)


def _active_event_messages(
    *,
    data: dict[str, Any] | None,
    agent_state: dict[str, Any],
    lockout: dict[str, Any] | None,
) -> dict[str, str]:
    active: dict[str, str] = {}

    if agent_state.get("stop_reason") == "circuit_breaker":
        reason = agent_state.get("last_error") or "unknown"
        active["circuit_breaker"] = f"Circuit breaker tripped: {reason}"

    if lockout is not None:
        reset_at = str(lockout.get("reset_at") or "unknown")
        reason = lockout.get("reason") or "Spotify lockout"
        active[f"lockout:{reset_at}"] = (
            f"Spotify quota lockout detected ({reason}) — resets {reset_at}"
        )

    if data:
        # Imported here, not at module scope: report imports agent, which
        # imports this module. A top-level import closes that cycle and breaks
        # `djsync report` entirely.
        from djsync.report import album_progress, playlist_progress

        pl_have, pl_total = playlist_progress(data)
        if pl_total > 0 and pl_have >= pl_total:
            active["playlists_complete"] = "All $d playlists are complete."

        al_have, al_total = album_progress(data)
        if al_total > 0 and al_have >= al_total:
            active["albums_complete"] = "All saved albums are complete."

        have, total = agent.library_progress(data)
        if total > 0 and have >= total:
            active["library_complete"] = "Library sync is complete."

    return active


def check_and_announce_events(
    *,
    data: dict[str, Any] | None = None,
    lockout: dict[str, Any] | None = None,
    now: datetime | None = None,
    urlopen: UrlopenFn | None = None,
) -> None:
    """Post Beeper alerts for significant agent events. Deduplicates via agent state."""
    _, _, token = get_beeper_settings()
    if not token:
        return

    now = now or datetime.now(UTC)
    state = agent.load_state()
    announced = _load_announced(state)
    active = _active_event_messages(data=data, agent_state=state, lockout=lockout)

    for key, message in active.items():
        if key in announced:
            continue
        if send_alert(
            title="djsync alert",
            body=message,
            now=now,
            urlopen=urlopen,
        ):
            announced.add(key)

    announced &= set(active)
    if announced != _load_announced(state):
        _save_announced(announced, now=now)


def drive_unmounted_over_24h(
    agent_state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    raw = agent_state.get("drive_unmounted_since")
    if not raw:
        return False
    try:
        since = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - since > timedelta(hours=24)


def parse_command(text: str) -> tuple[str, ...] | None:
    """Return a command tuple or None if *text* is not a recognised command."""
    stripped = text.strip()
    if not stripped:
        return None
    lower = stripped.casefold()
    if lower == "pause":
        return ("pause",)
    if lower == "resume":
        return ("resume",)
    if lower == "status":
        return ("status",)
    cap = _CAP_RE.match(stripped)
    if cap:
        return ("cap", cap.group(1))
    skip = _SKIP_RE.match(stripped)
    if skip:
        return ("skip", skip.group(1).strip())
    unskip = _UNSKIP_RE.match(stripped)
    if unskip:
        return ("unskip", unskip.group(1).strip())
    sync = _SYNC_RE.match(stripped)
    if sync:
        return ("sync", sync.group(1).strip())
    return None


def apply_command(
    cmd: tuple[str, ...],
    *,
    data: dict[str, Any] | None,
    now: datetime | None = None,
) -> str | None:
    """Apply *cmd* to agent state. Return reply text, or None for no reply."""
    now = now or datetime.now(UTC)
    state = agent.load_state()
    kind = cmd[0]

    if kind == "pause":
        agent.save_state({"paused": True}, now=now)
        return "paused"

    if kind == "resume":
        agent.save_state({"paused": False}, now=now)
        return "resumed"

    if kind == "status":
        if data is None:
            from djsync import cache as cache_mod

            data = cache_mod.load_cache()
        return agent.format_status_line(data, now=now)

    if kind == "cap":
        cap = int(cmd[1])
        agent.save_state({"daily_cap_override": cap}, now=now)
        return f"cap set to {cap}"

    if kind == "skip":
        name = cmd[1]
        skip_list = list(state.get("skip_list") or [])
        folded = name.casefold()
        if not any(str(s).casefold() == folded for s in skip_list):
            skip_list.append(name)
        agent.save_state({"skip_list": skip_list}, now=now)
        return f"skipped: {name}"

    if kind == "unskip":
        name = cmd[1]
        folded = name.casefold()
        skip_list = [
            s
            for s in (state.get("skip_list") or [])
            if str(s).casefold() != folded
        ]
        agent.save_state({"skip_list": skip_list}, now=now)
        return f"unskipped: {name}"

    if kind == "sync":
        name = cmd[1]
        agent.save_state({"sync_priority": name}, now=now)
        return f"sync priority: {name}"

    return None


def process_incoming_commands(
    *,
    data: dict[str, Any] | None = None,
    now: datetime | None = None,
    urlopen: UrlopenFn | None = None,
) -> None:
    """Read new chat messages and apply recognised commands. Never raises."""
    _, _, token = get_beeper_settings()
    if not token:
        return
    if not beeper_reachable(urlopen=urlopen):
        logger.warning("Beeper Desktop is not running; skipping command poll")
        record_delivery_failure("Beeper Desktop not running")
        return

    state = agent.load_state()
    last_id = str(state.get("beeper_last_message_id") or "")
    messages = fetch_messages(urlopen=urlopen)
    if not messages:
        return

    new_messages = [
        m
        for m in messages
        if last_id == "" or _message_id_key(str(m.get("id") or "")) > _message_id_key(last_id)
    ]
    if not new_messages:
        return

    latest_id = last_id
    for message in new_messages:
        msg_id = str(message.get("id") or "")
        if msg_id:
            latest_id = msg_id
        text = _extract_message_text(message)
        cmd = parse_command(text)
        if cmd is None:
            continue
        reply = apply_command(cmd, data=data, now=now)
        if reply:
            send_message(reply, urlopen=urlopen)

    if latest_id and latest_id != last_id:
        agent.save_state({"beeper_last_message_id": latest_id}, now=now)
