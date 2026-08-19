# djsync

Sync a DJ's Spotify playlists to an external drive as MP3s.

## Phase 0 (current)

Phase 0 is the **read-side skeleton** only. It can:

- Parse **sigils** from playlist names (e.g. `$d` marks a playlist for sync)
- Load Spotify credentials from `.env`
- List all of your Spotify playlists via the API
- Fetch tracks for a playlist (library code; not exposed in the CLI yet)

It does **not** download audio, search YouTube, match tracks, or write tags — those come in later phases.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13.

```bash
uv sync
```

### Spotify app credentials

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in.
2. Click **Create app**. Name it (e.g. `djsync`) and accept the terms.
3. Open the app → **Settings**.
4. Copy **Client ID** and **Client secret**.
5. Under **Redirect URIs**, add: `http://127.0.0.1:8888/callback` and save.
6. Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

## Usage

List playlists (requires `.env`):

```bash
uv run djsync playlists
```

JSON output:

```bash
uv run djsync playlists --json
```

Run tests:

```bash
uv run pytest
```

## Sigils

Playlist names can include sigils: `$` followed by one or more letters as their own token. Example: `thug scott - $d` contains sigil `d`. Playlists marked with `$d` are intended sync targets in later phases.
