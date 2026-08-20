"""Command-line interface for djsync."""

from __future__ import annotations

import json
import sys

import click

from djsync import cache, config, spotify
from djsync.sync import sync_playlist


def _exit_on_spotify_error(exc: BaseException) -> None:
    if isinstance(exc, spotify.RateLimitedError):
        click.echo(str(exc), err=True)
        sys.exit(1)
    if isinstance(exc, (RuntimeError, cache.CacheDataError)):
        click.echo(str(exc), err=True)
        sys.exit(1)
    raise exc


def _playlist_list_from_cache(*, refresh: bool) -> list[spotify.Playlist]:
    try:
        client = spotify.get_client()
    except RuntimeError as exc:
        _exit_on_spotify_error(exc)

    if refresh:
        try:
            data = cache.refresh_playlist_catalog(client, prior=cache.load_cache())
            cache.save_cache(data)
            return cache.playlists_from_catalog(data)
        except spotify.RateLimitedError as exc:
            _exit_on_spotify_error(exc)

    cached = cache.load_cache()
    if cached and cached.get("playlist_catalog"):
        return cache.playlists_from_catalog(cached)

    if cached and cached.get("playlists"):
        return [
            cache.playlist_from_entry(entry)
            for entry in cached["playlists"]
            if "d" in (entry.get("sigils") or [])
        ]

    raise cache.CacheDataError(
        "No cached playlist catalog. Run `djsync refresh` first."
    )


@click.group()
def main() -> None:
    """Sync DJ Spotify playlists to an external drive."""


@main.command("playlists")
@click.option("--json", "as_json", is_flag=True, help="Output playlist data as JSON.")
@click.option(
    "--refresh",
    is_flag=True,
    help="Fetch the playlist list from Spotify and update the cache.",
)
def playlists_cmd(as_json: bool, refresh: bool) -> None:
    """List all Spotify playlists with parsed sigils."""
    try:
        playlist_list = _playlist_list_from_cache(refresh=refresh)
    except cache.CacheDataError as exc:
        _exit_on_spotify_error(exc)
    except RuntimeError as exc:
        _exit_on_spotify_error(exc)

    marked_d = [p for p in playlist_list if "d" in p.sigils]

    if as_json:
        payload = [
            {
                "id": p.id,
                "name": p.name,
                "track_count": p.track_count,
                "sigils": sorted(p.sigils),
                "marked_d": "d" in p.sigils,
                "snapshot_id": p.snapshot_id,
            }
            for p in playlist_list
        ]
        click.echo(json.dumps(payload, indent=2))
    else:
        for p in playlist_list:
            sigil_text = ", ".join(f"${s}" for s in sorted(p.sigils)) or "(none)"
            marker = " [SYNC $d]" if "d" in p.sigils else ""
            click.echo(f"{p.name}  ({p.track_count} tracks)  sigils: {sigil_text}{marker}")

    if not as_json:
        click.echo(f"\n{len(playlist_list)} playlists, {len(marked_d)} marked $d")


@main.command("refresh")
@click.option(
    "--max-playlists",
    type=int,
    default=None,
    help="Fetch tracks for at most N changed playlists this refresh.",
)
def refresh_cmd(max_playlists: int | None) -> None:
    """Rebuild the library cache from Spotify (reuses unchanged playlists)."""
    try:
        client = spotify.get_client()
        data = cache.build_cache(
            client,
            max_playlists=max_playlists,
            prior=cache.load_cache(),
            on_log=lambda msg: click.echo(msg, err=True),
        )
        cache.save_cache(data)
    except spotify.RateLimitedError as exc:
        _exit_on_spotify_error(exc)
    except RuntimeError as exc:
        _exit_on_spotify_error(exc)

    click.echo(f"Cache refreshed at {data['timestamp']}")


@main.command("sync")
@click.option(
    "--playlist",
    required=True,
    help="Exact Spotify playlist name (among $d playlists).",
)
@click.option("--dry-run", is_flag=True, help="Search and rank only; do not download.")
@click.option("--limit", type=int, default=None, help="Process at most N missing tracks.")
@click.option(
    "--refresh",
    is_flag=True,
    help="Fetch playlist metadata and tracks from Spotify instead of using cache.",
)
def sync_cmd(playlist: str, dry_run: bool, limit: int | None, refresh: bool) -> None:
    """Match, download, and tag tracks for a $d playlist."""
    try:
        client = spotify.get_client()
        cached = cache.load_cache()
        target, tracks = cache.resolve_playlist_for_sync(
            client,
            playlist,
            refresh=refresh,
            cached=cached,
        )
    except cache.CacheDataError as exc:
        _exit_on_spotify_error(exc)
    except ValueError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except RuntimeError as exc:
        _exit_on_spotify_error(exc)
    except spotify.RateLimitedError as exc:
        _exit_on_spotify_error(exc)

    def on_log(msg: str) -> None:
        click.echo(msg, err=msg.startswith("FAIL"))

    try:
        result = sync_playlist(
            client,
            target,
            dry_run=dry_run,
            limit=limit,
            tracks=tracks,
            on_log=on_log,
            on_track=lambda name: None,
        )
    except spotify.RateLimitedError as exc:
        _exit_on_spotify_error(exc)

    click.echo(
        f"\nSummary: downloaded={result.downloaded} skipped={result.skipped} "
        f"failed={result.failed}"
        + (" (dry-run)" if dry_run else "")
    )
    if result.unverified_explicit:
        click.echo(
            "\nExplicit tracks — verify version (may be clean):"
        )
        for entry in result.unverified_explicit:
            artists = ", ".join(entry["artists"])
            click.echo(f"  {artists} — {entry['name']}")
            click.echo(f"       -> {entry['chosen_title']}")


@main.command("agent")
@click.option("--dry-run", is_flag=True, help="Plan only; do not download.")
@click.option(
    "--max",
    "max_downloads",
    type=int,
    default=None,
    help="Override this run's download allowance (still capped by the 24h ledger).",
)
def agent_cmd(dry_run: bool, max_downloads: int | None) -> None:
    """Unattended one-shot sync. Quiet when the drive is missing or locked out."""
    import logging

    from djsync.agent import run_agent

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        code = run_agent(dry_run=dry_run, max_downloads=max_downloads)
    except Exception:
        logging.getLogger("djsync.agent").exception("agent failed")
        sys.exit(1)
    sys.exit(code)


@main.command("ui")
def ui_cmd() -> None:
    """Launch the local web UI for choosing playlists to sync."""
    from djsync.web import run_server

    try:
        config.load_config()
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    run_server()


if __name__ == "__main__":
    main()
