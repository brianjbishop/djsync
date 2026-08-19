"""Local web UI for choosing and syncing playlists and albums."""

from __future__ import annotations

import socket
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import spotipy
from flask import Flask, jsonify, request, send_from_directory

from djsync import cache, genres, selection, spotify, sync
from djsync.config import PROJECT_ROOT, get_destination
from djsync.web_helpers import format_size

STATIC_DIR = Path(__file__).resolve().parent / "static"

SyncKind = Literal["playlists", "albums"]


def _destination_payload() -> dict[str, Any]:
    dest = get_destination()
    free = dest.free_bytes()
    return {
        "drive": str(dest.drive),
        "library_root": dest.library_root,
        "playlists_dir": dest.playlists_dir,
        "albums_dir": dest.albums_dir,
        "path": str(dest.path),
        "playlists_path": str(dest.path_for("playlists")),
        "albums_path": str(dest.path_for("albums")),
        "mounted": dest.mounted,
        "exists": dest.exists,
        "free_bytes": free,
        "free_human": format_size(free) if free is not None else None,
    }


@dataclass
class SyncJobState:
    running: bool = False
    kind: SyncKind | None = None
    current_item: str | None = None
    current_track: str | None = None
    done: int = 0
    total: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    log: list[str] = field(default_factory=list)
    unverified_explicit: list[dict[str, str | list[str]]] = field(default_factory=list)


@dataclass
class GenreJobState:
    running: bool = False
    done: int = 0
    total: int = 0


_job = SyncJobState()
_job_lock = threading.Lock()
_genre_job = GenreJobState()
_genre_lock = threading.Lock()


def _merge_playlists(cache_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach selection and genre fields to cached playlist rows."""
    selected = selection.load_selection("playlists")
    genre_map = genres.load_genres()
    rows: list[dict[str, Any]] = []
    for p in cache_data.get("playlists") or []:
        row = dict(p)
        row["selected"] = row["id"] in selected
        row["genre"] = genre_map.get(row["id"])
        rows.append(row)
    return rows


def _merge_albums(cache_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach selection fields to cached album rows."""
    selected = selection.load_selection("albums")
    rows: list[dict[str, Any]] = []
    for album in cache_data.get("albums") or []:
        row = dict(album)
        row["selected"] = row["id"] in selected
        rows.append(row)
    return rows


def _cache_response(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": data.get("timestamp"),
        "playlists": _merge_playlists(data),
        "albums": _merge_albums(data),
        "destination": _destination_payload(),
    }


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR))

    @app.get("/")
    def index() -> Any:
        response = send_from_directory(STATIC_DIR, "index.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/destination")
    def api_destination() -> Any:
        return jsonify(_destination_payload())

    @app.get("/api/playlists")
    def api_playlists() -> Any:
        refresh = request.args.get("refresh") == "1"
        try:
            client = spotify.get_client()
            data = cache.get_or_build_cache(client, refresh=refresh)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500
        payload = _cache_response(data)
        return jsonify(
            {
                "timestamp": payload["timestamp"],
                "playlists": payload["playlists"],
                "destination": payload["destination"],
            }
        )

    @app.get("/api/albums")
    def api_albums() -> Any:
        refresh = request.args.get("refresh") == "1"
        try:
            client = spotify.get_client()
            data = cache.get_or_build_cache(client, refresh=refresh)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500
        payload = _cache_response(data)
        return jsonify(
            {
                "timestamp": payload["timestamp"],
                "albums": payload["albums"],
                "destination": payload["destination"],
            }
        )

    @app.post("/api/refresh")
    def api_refresh() -> Any:
        try:
            client = spotify.get_client()
            data = cache.build_cache(client)
            cache.save_cache(data)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(_cache_response(data))

    @app.post("/api/selection")
    def api_selection() -> Any:
        body = request.get_json(silent=True) or {}
        ids = body.get("selected")
        if not isinstance(ids, list):
            return jsonify({"error": "expected {selected: [...]}"}), 400
        kind_raw = body.get("kind", "playlists")
        if kind_raw not in ("playlists", "albums"):
            return jsonify({"error": "kind must be playlists or albums"}), 400
        kind: SyncKind = kind_raw
        selection.save_selection({str(i) for i in ids}, kind=kind)
        return jsonify({"ok": True, "count": len(ids), "kind": kind})

    @app.post("/api/genres")
    def api_genres() -> Any:
        with _genre_lock:
            if _genre_job.running:
                return jsonify({"error": "genre fetch already running"}), 409

        body = request.get_json(silent=True) or {}
        playlist_ids = body.get("playlist_ids")
        if playlist_ids is None:
            cached = cache.load_cache()
            if cached is None:
                return jsonify({"error": "no cache; refresh playlists first"}), 400
            playlist_ids = [p["id"] for p in cached.get("playlists") or []]
        if not isinstance(playlist_ids, list):
            return jsonify({"error": "expected playlist_ids list"}), 400

        def run() -> None:
            with _genre_lock:
                _genre_job.running = True
                _genre_job.done = 0
                _genre_job.total = len(playlist_ids)

            try:
                client = spotify.get_client()

                def on_progress(done: int, total: int, _pid: str) -> None:
                    with _genre_lock:
                        _genre_job.done = done
                        _genre_job.total = total

                genres.fetch_all_genres(
                    client,
                    [str(i) for i in playlist_ids],
                    on_progress=on_progress,
                )
            except RuntimeError:
                pass
            finally:
                with _genre_lock:
                    _genre_job.running = False

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"ok": True, "total": len(playlist_ids)})

    @app.get("/api/genres/status")
    def api_genres_status() -> Any:
        with _genre_lock:
            return jsonify(
                {
                    "running": _genre_job.running,
                    "done": _genre_job.done,
                    "total": _genre_job.total,
                }
            )

    @app.post("/api/sync")
    def api_sync() -> Any:
        with _job_lock:
            if _job.running:
                return jsonify({"error": "sync already running"}), 409

        dest = get_destination()
        if not dest.mounted:
            return jsonify(
                {"error": f"Drive not connected: {dest.drive}"}
            ), 409

        body = request.get_json(silent=True) or {}
        kind_raw = body.get("kind", "playlists")
        if kind_raw not in ("playlists", "albums"):
            return jsonify({"error": "kind must be playlists or albums"}), 400
        kind: SyncKind = kind_raw

        if kind == "albums":
            item_ids = body.get("album_ids")
            if not isinstance(item_ids, list) or not item_ids:
                return jsonify({"error": "expected non-empty album_ids"}), 400
        else:
            item_ids = body.get("playlist_ids")
            if not isinstance(item_ids, list) or not item_ids:
                return jsonify({"error": "expected non-empty playlist_ids"}), 400

        cached = cache.load_cache()
        if cached is None:
            return jsonify({"error": "no cache; refresh first"}), 400

        if kind == "albums":
            by_id = {a["id"]: a for a in cached.get("albums") or []}
            targets: list[Any] = []
            for aid in item_ids:
                entry = by_id.get(str(aid))
                if entry is None:
                    continue
                targets.append(
                    spotify.Album(
                        id=entry["id"],
                        name=entry["name"],
                        artists=tuple(entry.get("artists") or []),
                        total_tracks=entry["total_tracks"],
                        release_date=entry.get("release_date") or "",
                        added_at=entry.get("added_at") or "",
                        spotify_url=entry.get("spotify_url")
                        or f"https://open.spotify.com/album/{entry['id']}",
                        spotify_uri=entry.get("spotify_uri")
                        or f"spotify:album:{entry['id']}",
                    )
                )
            count_key = "total_tracks"
        else:
            by_id = {p["id"]: p for p in cached.get("playlists") or []}
            targets = []
            for pid in item_ids:
                entry = by_id.get(str(pid))
                if entry is None:
                    continue
                targets.append(
                    spotify.Playlist(
                        id=entry["id"],
                        name=entry["name"],
                        track_count=entry["track_count"],
                        sigils=frozenset(entry.get("sigils") or []),
                    )
                )
            count_key = "track_count"

        if not targets:
            label = "albums" if kind == "albums" else "playlists"
            return jsonify({"error": f"no matching {label}"}), 400

        job_id = "sync"

        def run_sync() -> None:
            with _job_lock:
                _job.running = True
                _job.kind = kind
                _job.current_item = None
                _job.current_track = None
                _job.done = 0
                _job.total = sum(
                    max(
                        0,
                        by_id[str(iid)][count_key]
                        - by_id[str(iid)]["downloaded_count"],
                    )
                    for iid in item_ids
                    if str(iid) in by_id
                )
                _job.downloaded = 0
                _job.skipped = 0
                _job.failed = 0
                _job.log = []
                _job.unverified_explicit = []

            try:
                client = spotify.get_client()
                for target in targets:
                    name = target.name if kind == "playlists" else target.name
                    with _job_lock:
                        _job.current_item = name

                    def on_log(msg: str) -> None:
                        with _job_lock:
                            _job.log.append(msg)
                            if len(_job.log) > 200:
                                _job.log = _job.log[-200:]

                    def on_track(track_name: str) -> None:
                        with _job_lock:
                            _job.current_track = track_name

                    def on_progress() -> None:
                        with _job_lock:
                            _job.done += 1

                    if kind == "albums":
                        result = sync.sync_album(
                            client,
                            target,
                            on_log=on_log,
                            on_track=on_track,
                            on_progress=on_progress,
                        )
                    else:
                        result = sync.sync_playlist(
                            client,
                            target,
                            on_log=on_log,
                            on_track=on_track,
                            on_progress=on_progress,
                        )
                    with _job_lock:
                        _job.downloaded += result.downloaded
                        _job.skipped += result.skipped
                        _job.failed += result.failed
                        _job.unverified_explicit.extend(result.unverified_explicit)
            except RuntimeError as exc:
                with _job_lock:
                    _job.log.append(f"ERROR: {exc}")
            finally:
                with _job_lock:
                    _job.running = False
                    _job.kind = None
                    _job.current_item = None
                    _job.current_track = None

        threading.Thread(target=run_sync, daemon=True).start()
        return jsonify({"job_id": job_id, "kind": kind})

    @app.get("/api/status")
    def api_status() -> Any:
        with _job_lock:
            return jsonify(
                {
                    "running": _job.running,
                    "kind": _job.kind,
                    "current_playlist": _job.current_item,
                    "current_item": _job.current_item,
                    "current_track": _job.current_track,
                    "done": _job.done,
                    "total": _job.total,
                    "downloaded": _job.downloaded,
                    "skipped": _job.skipped,
                    "failed": _job.failed,
                    "log": list(_job.log),
                    "unverified_explicit": list(_job.unverified_explicit),
                }
            )

    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server(*, open_browser: bool = True) -> None:
    """Start the local web UI."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    app = create_app()
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    # Record the URL so the Finder launcher can reopen a running instance
    # instead of starting a second server.
    try:
        state = Path.home() / ".djsync"
        state.mkdir(parents=True, exist_ok=True)
        (state / "url").write_text(url)
    except OSError:
        pass

    print(f"djsync UI at {url}")
    print(f"Project root: {PROJECT_ROOT}")
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
