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
    "explicit_match": 0.75,
    "clean_mismatch": 0.4,
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = PROJECT_ROOT / "fixtures"


def fixtures_dir() -> Path:
    """Where match fixtures are written.

    Resolved at call time, not import time, so the test suite can redirect it.
    The fixture corpus is the basis for offline matcher replay - synthetic test
    records written into it would silently skew any match-rate analysis.
    """
    override = os.getenv("DJSYNC_FIXTURES_DIR")
    return Path(override) if override else FIXTURES_DIR

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
# Downloads authenticate as a logged-in YouTube session. Value is
# "browser" or "browser:Profile Name" - keep this pointed at a profile whose
# account you would not mind losing, since bulk downloading risks a ban.
COOKIES_FROM_BROWSER: str | None = os.getenv(
    "DJSYNC_COOKIES_FROM_BROWSER", "chrome:Profile 2"
)
REMOTE_COMPONENTS = ["ejs:github"]

# Downloading runs against a real logged-in account, so pace it. Bursts of
# hundreds of requests are the pattern that gets accounts rate-limited; a few
# seconds between tracks costs little and looks nothing like a scraper.
SLEEP_BETWEEN_DOWNLOADS = (3, 8)  # random delay range, seconds

# Spotify API usage budgets (local ledger; Spotify publishes no quota endpoint).
# ~788 requests in ~4 minutes triggered a ~24h lockout on a development-mode app;
# these defaults are deliberately conservative and all env-overridable.
DAILY_REQUEST_BUDGET = int(os.getenv("DJSYNC_DAILY_REQUEST_BUDGET", "300"))
BURST_PER_30S = int(os.getenv("DJSYNC_BURST_PER_30S", "15"))
SPOTIFY_MIN_REQUEST_INTERVAL = float(
    os.getenv("DJSYNC_SPOTIFY_MIN_REQUEST_INTERVAL", "1.5")
)

# YouTube download cap (rolling 24h) and unattended-agent knobs.
DAILY_DOWNLOAD_CAP = int(os.getenv("DJSYNC_DAILY_DOWNLOAD_CAP", "800"))
STALE_AFTER_HOURS = float(os.getenv("DJSYNC_STALE_AFTER_HOURS", "12"))
CIRCUIT_BREAKER_FAILURES = int(os.getenv("DJSYNC_CIRCUIT_BREAKER_FAILURES", "5"))
PLAYLISTS_PER_RUN = int(os.getenv("DJSYNC_PLAYLISTS_PER_RUN", "10"))
SYNC_ALBUMS = os.getenv("DJSYNC_SYNC_ALBUMS", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)

DJSYNC_REPORT_EMAIL = os.getenv("DJSYNC_REPORT_EMAIL", "brian.rio11@gmail.com")


def get_beeper_settings() -> tuple[str, str, str]:
    """Return (base_url, chat_id, token) from the environment."""
    load_dotenv()
    return (
        os.getenv("DJSYNC_BEEPER_URL", "http://127.0.0.1:23373"),
        os.getenv("DJSYNC_BEEPER_CHAT_ID", "33169"),
        os.getenv("DJSYNC_BEEPER_TOKEN", ""),
    )


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
        browser, _, profile = COOKIES_FROM_BROWSER.partition(":")
        opts["cookiesfrombrowser"] = (browser, profile or None, None, None)
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
