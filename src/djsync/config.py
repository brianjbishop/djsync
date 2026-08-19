"""Load Spotify credentials and drive paths from the environment."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

WEIGHTS: dict[str, float] = {
    "duration_delta": 4.0,
    "art_track": 6.0,
    "title_similarity": 3.0,
    "artist_match": 2.0,
    "negative_flags": -2.5,
    "view_count": 0.15,
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = PROJECT_ROOT / "fixtures"

_DEFAULT_DRIVE = "/Volumes/BRIANB"
_DEFAULT_LIBRARY_ROOT = "dj"
_DEFAULT_PLAYLISTS_DIR = "playlists"
_DEFAULT_ALBUMS_DIR = "albums"

CollectionKind = Literal["playlists", "albums"]

# --- YouTube access -------------------------------------------------------
# YouTube blocks anonymous yt-dlp clients outright (HTTP 403), and gates the
# rest behind a JS signature challenge. Two things are needed to get audio:
#   1. COOKIES_FROM_BROWSER - authenticates the request as a real logged-in
#      session. yt-dlp reads the browser's cookie store directly.
#   2. REMOTE_COMPONENTS - yt-dlp's official EJS solver, run under Deno, which
#      computes YouTube's signature / "n" parameter values.
# Set COOKIES_FROM_BROWSER to None to attempt anonymous downloads.
COOKIES_FROM_BROWSER: str | None = "chrome"
REMOTE_COMPONENTS = ["ejs:github"]

# Downloading runs against a real logged-in account, so pace it. Bursts of
# hundreds of requests are the pattern that gets accounts rate-limited; a few
# seconds between tracks costs little and looks nothing like a scraper.
SLEEP_BETWEEN_DOWNLOADS = (3, 8)  # random delay range, seconds


@dataclass(frozen=True)
class Destination:
    """Resolved sync destination on an external drive."""

    drive: Path
    library_root: str
    playlists_dir: str
    albums_dir: str

    @property
    def path(self) -> Path:
        """Playlists collection path (backwards-compatible alias)."""
        return self.path_for("playlists")

    def path_for(self, kind: CollectionKind) -> Path:
        subdir = self.playlists_dir if kind == "playlists" else self.albums_dir
        return self.drive / self.library_root / subdir

    @property
    def mounted(self) -> bool:
        return self.drive.is_dir() and os.path.ismount(self.drive)

    @property
    def exists(self) -> bool:
        return self.path.is_dir()

    def free_bytes(self) -> int | None:
        if not self.mounted:
            return None
        try:
            return shutil.disk_usage(self.drive).free
        except OSError:
            return None


def get_destination() -> Destination:
    """Build the sync destination from environment variables."""
    load_dotenv()
    drive = Path(os.getenv("DJSYNC_DRIVE", _DEFAULT_DRIVE))
    library_root = os.getenv("DJSYNC_LIBRARY_ROOT", _DEFAULT_LIBRARY_ROOT)
    playlists_dir = os.getenv("DJSYNC_PLAYLISTS_DIR", _DEFAULT_PLAYLISTS_DIR)
    albums_dir = os.getenv("DJSYNC_ALBUMS_DIR", _DEFAULT_ALBUMS_DIR)
    # DJSYNC_COLLECTION is retired; honour it only when the new var is unset.
    legacy = os.getenv("DJSYNC_COLLECTION")
    if legacy and not os.getenv("DJSYNC_PLAYLISTS_DIR"):
        playlists_dir = legacy
    return Destination(
        drive=drive,
        library_root=library_root,
        playlists_dir=playlists_dir,
        albums_dir=albums_dir,
    )


def __getattr__(name: str) -> object:
    """Backwards-compatible drive path aliases."""
    dest = get_destination()
    if name == "PLAYLISTS_DIR":
        return dest.path
    if name == "DRIVE_PATH":
        return dest.drive
    if name == "DRIVE_NAME":
        return dest.drive.name
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def youtube_opts() -> dict:
    """Return the shared yt-dlp options for reaching YouTube."""
    opts: dict = {"remote_components": list(REMOTE_COMPONENTS)}
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)
    return opts

_REQUIRED_VARS = (
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REDIRECT_URI",
)


def _missing_credentials_message() -> str:
    return (
        "Spotify credentials are not configured.\n\n"
        "Copy .env.example to .env and fill in your Spotify app credentials:\n"
        "  cp .env.example .env\n\n"
        "Create a Spotify app at https://developer.spotify.com/dashboard\n"
        "and set the redirect URI to match SPOTIFY_REDIRECT_URI in .env."
    )


def load_config() -> dict[str, str]:
    """Load and validate Spotify credentials from .env."""
    load_dotenv()

    missing = [var for var in _REQUIRED_VARS if not os.getenv(var)]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(
            f"Missing required environment variable(s): {names}.\n\n"
            f"{_missing_credentials_message()}"
        )

    return {var: os.environ[var] for var in _REQUIRED_VARS}
