# djsync

Keeps a DJ library on an external drive in sync with Spotify.

Playlists are curated in Spotify; the audio has to exist as real files on the drive that
Serato and Rekordbox read. djsync closes that gap: plug in the drive, run it, and the
playlists and albums you marked show up as tagged MP3s.

## What it syncs

**Playlists** marked with a `$d` sigil in their name. Playlist names carry a small
vocabulary of `$`-prefixed markers (`$d`, `$log`, `$ave`, `$track`, ...); djsync parses all
of them and acts on `$d`. Marking a playlist for sync is just renaming it in Spotify.

**Albums** saved to your Spotify library. Saved albums cannot be renamed, so there is no
sigil equivalent — you pick which ones to sync in the UI.

Sync is **additive only**. Nothing is ever deleted or overwritten. Serato and Rekordbox
crates reference file paths, so a deletion breaks a crate silently, possibly mid-set.
Tracks that disappear from a playlist are reported, never removed.

## Layout on the drive

```
<drive>/<library>/playlists/<exact playlist name>/<track name>.mp3
<drive>/<library>/albums/<artist> - <album>/<NN> <track name>.mp3
```

Playlist folders match the Spotify playlist name verbatim (only `/` is substituted, being
illegal in filenames). Album tracks are zero-padded and numbered so album order survives in
Finder and in the DJ software; multi-disc albums get a `<disc>-<NN>` prefix.

Configure the destination in `.env`:

```
DJSYNC_DRIVE=/Volumes/BRIANB
DJSYNC_LIBRARY_ROOT=dj
DJSYNC_PLAYLISTS_DIR=playlists
DJSYNC_ALBUMS_DIR=albums
```

## How matching works

Spotify's API never exposes audio, so each track has to be found elsewhere. That matching
is the interesting part of this project, and the part most likely to be wrong.

For each track djsync searches YouTube, collects candidates, and scores them:

| Signal | Why |
|---|---|
| Duration delta | Strongest. Within ~2s of Spotify's duration is near-conclusive. |
| Art Track / Topic channel | Auto-generated from the label's own release. |
| Title similarity | After stripping `official video`, `lyrics`, `HD`, and similar noise. |
| Artist match | Against both channel name and title. |
| Negative flags | `sped up`, `slowed`, `live`, `cover`, `karaoke`, and `remix` when the Spotify title has none. |
| Explicit match | Breaks ties toward the explicit cut when Spotify marks the track explicit. |
| View count | Weak tiebreaker only. Weighting it heavily surfaces TikTok edits over originals. |

Album tracks additionally search with the album name appended, because the album cut and the
single often share a title but not a length.

`matcher/score.py` is deliberately pure — no network, no file I/O. That is what makes the
scoring replayable (see below).

### Every match is recorded

Each match attempt writes a fixture to `fixtures/` containing the Spotify metadata, the pick,
and **the full list of candidates it was choosing between**, with their durations, channels,
and view counts.

This exists because YouTube search results drift. A candidate list not captured at match time
is unrecoverable, so a recorded verdict alone ("this was wrong") could never be tested against
a scoring change without re-querying. With the candidates stored, any change to the scorer can
be replayed against the entire history of past decisions offline, deterministically, in
milliseconds — which is the only way to change the matcher without silently regressing
matches that were already correct.

## Known limitations

**Clean versions.** YouTube frequently only carries the censored cut of explicit tracks,
including on official Topic channels, since clean audio monetizes better. djsync searches
specifically for explicit versions when Spotify flags a track, and prefers them in scoring,
but cannot guarantee one exists. Tracks where the winning candidate carried no explicit
marker are listed after each sync as worth checking. There is no reliable automated way to
verify this from the audio — silence detection does not work, because modern clean edits
reverse or pitch-shift words rather than muting them.

**YouTube blocks anonymous downloads.** Getting audio requires browser cookies plus yt-dlp's
EJS challenge solver running under Deno. Downloads are paced with a delay between tracks.

**Nothing appears in Serato or Rekordbox automatically.** Files land on the drive correctly,
but Serato needs the folder added to its library and Rekordbox needs an import and analysis
pass. Generating crates and a Rekordbox XML is not implemented yet.

## Setup

Requires Python 3.13, [uv](https://docs.astral.sh/uv/), ffmpeg, and Deno.

```bash
brew install ffmpeg deno
uv sync
```

Create an app at [developer.spotify.com](https://developer.spotify.com/dashboard), add
`http://127.0.0.1:8888/callback` as a redirect URI, then:

```bash
cp .env.example .env   # fill in your client id and secret
```

Reading saved albums needs the `user-library-read` scope, so Spotify will prompt for
authorization on first run.

## Usage

```bash
./djsync-run playlists                      # list playlists and their sigils
./djsync-run sync --playlist "name | $d"    # sync one playlist
./djsync-run sync --playlist "name | $d" --dry-run   # show picks, download nothing
./djsync-run ui                             # local web UI
```

The UI is the main surface: it lists playlists and saved albums with their download status,
lets you select what to sync, groups by sigil or artist, sorts by name, size, or date added,
links each item back to Spotify, and shows live progress during a sync. `djsync.app`
launches it from Finder.

## Status

Playlists and albums both sync end to end. Not yet built: automatic sync on drive mount,
Serato/Rekordbox crate generation, and using the fixture corpus to tune the matcher
automatically.
