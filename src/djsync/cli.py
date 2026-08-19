"""Command-line interface for djsync."""

from __future__ import annotations

import json
import sys

import click

from djsync import config, spotify
from djsync.config import FIXTURES_DIR, PLAYLISTS_DIR
from djsync.sync import sync_playlist


@click.group()
def main() -> None:
    """Sync DJ Spotify playlists to an external drive."""


@main.command("playlists")
@click.option("--json", "as_json", is_flag=True, help="Output playlist data as JSON.")
def playlists_cmd(as_json: bool) -> None:
    """List all Spotify playlists with parsed sigils."""
    try:
        client = spotify.get_client()
        playlist_list = spotify.fetch_playlists(client)
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    marked_d = [p for p in playlist_list if "d" in p.sigils]

    if as_json:
        payload = [
            {
                "id": p.id,
                "name": p.name,
                "track_count": p.track_count,
                "sigils": sorted(p.sigils),
                "marked_d": "d" in p.sigils,
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


@main.command("sync")
@click.option(
    "--playlist",
    required=True,
    help="Exact Spotify playlist name (among $d playlists).",
)
@click.option("--dry-run", is_flag=True, help="Search and rank only; do not download.")
@click.option("--limit", type=int, default=None, help="Process at most N missing tracks.")
def sync_cmd(playlist: str, dry_run: bool, limit: int | None) -> None:
    """Match, download, and tag tracks for a $d playlist."""
    try:
        client = spotify.get_client()
        playlist_list = spotify.fetch_playlists(client)
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    marked = [p for p in playlist_list if "d" in p.sigils]
    matches = [p for p in marked if p.name == playlist]
    if not matches:
        click.echo(
            f'No $d playlist named "{playlist}" found.\n'
            f"Use `djsync playlists` to list available names.",
            err=True,
        )
        sys.exit(1)
    if len(matches) > 1:
        ids = ", ".join(p.id for p in matches)
        click.echo(
            f'Ambiguous playlist name "{playlist}" ({len(matches)} matches: {ids}).',
            err=True,
        )
        sys.exit(1)

    target = matches[0]

    def on_log(msg: str) -> None:
        click.echo(msg, err=msg.startswith("FAIL"))

    result = sync_playlist(
        client,
        target,
        dry_run=dry_run,
        limit=limit,
        on_log=on_log,
        on_track=lambda name: None,
    )

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
