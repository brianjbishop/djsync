"""Local web UI for choosing and syncing playlists."""

from __future__ import annotations

import socket
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import spotipy
from flask import Flask, jsonify, request, send_from_directory

from djsync import cache, genres, selection, spotify, sync
from djsync.config import PROJECT_ROOT, get_destination
from djsync.web_helpers import format_size

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _destination_payload() -> dict[str, Any]:
    dest = get_destination()
    free = dest.free_bytes()
    return {
        "drive": str(dest.drive),
        "library_root": dest.library_root,
        "collection": dest.collection,
        "path": str(dest.path),
        "mounted": dest.mounted,
        "exists": dest.exists,
        "free_bytes": free,
        "free_human": format_size(free) if free is not None else None,
    }


@dataclass
class SyncJobState:
    running: bool = False
    current_playlist: str | None = None
    current_track: str | None = None
    done: int = 0
    total: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    log: list[str] = field(default_factory=list)


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
    selected = selection.load_selection()
    genre_map = genres.load_genres()
    rows: list[dict[str, Any]] = []
    for p in cache_data.get("playlists") or []:
        row = dict(p)
        row["selected"] = row["id"] in selected
        row["genre"] = genre_map.get(row["id"])
        rows.append(row)
    return rows


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
        return jsonify(
            {
                "timestamp": data.get("timestamp"),
                "playlists": _merge_playlists(data),
                "destination": _destination_payload(),
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
        return jsonify(
            {
                "timestamp": data.get("timestamp"),
                "playlists": _merge_playlists(data),
                "destination": _destination_payload(),
            }
        )

    @app.post("/api/selection")
    def api_selection() -> Any:
        body = request.get_json(silent=True) or {}
        ids = body.get("selected")
        if not isinstance(ids, list):
            return jsonify({"error": "expected {selected: [...]}"}), 400
        selection.save_selection({str(i) for i in ids})
        return jsonify({"ok": True, "count": len(ids)})

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
        playlist_ids = body.get("playlist_ids")
        if not isinstance(playlist_ids, list) or not playlist_ids:
            return jsonify({"error": "expected non-empty playlist_ids"}), 400

        cached = cache.load_cache()
        if cached is None:
            return jsonify({"error": "no cache; refresh playlists first"}), 400

        by_id = {p["id"]: p for p in cached.get("playlists") or []}
        targets = []
        for pid in playlist_ids:
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

        if not targets:
            return jsonify({"error": "no matching playlists"}), 400

        job_id = "sync"

        def run_sync() -> None:
            with _job_lock:
                _job.running = True
                _job.current_playlist = None
                _job.current_track = None
                _job.done = 0
                _job.total = sum(
                    max(0, by_id[str(pid)]["track_count"] - by_id[str(pid)]["downloaded_count"])
                    for pid in playlist_ids
                    if str(pid) in by_id
                )
                _job.downloaded = 0
                _job.skipped = 0
                _job.failed = 0
                _job.log = []

            try:
                client = spotify.get_client()
                for pl in targets:
                    with _job_lock:
                        _job.current_playlist = pl.name

                    def on_log(msg: str) -> None:
                        with _job_lock:
                            _job.log.append(msg)
                            if len(_job.log) > 200:
                                _job.log = _job.log[-200:]

                    def on_track(name: str) -> None:
                        with _job_lock:
                            _job.current_track = name

                    def on_progress() -> None:
                        with _job_lock:
                            _job.done += 1

                    result = sync.sync_playlist(
                        client,
                        pl,
                        on_log=on_log,
                        on_track=on_track,
                        on_progress=on_progress,
                    )
                    with _job_lock:
                        _job.downloaded += result.downloaded
                        _job.skipped += result.skipped
                        _job.failed += result.failed
            except RuntimeError as exc:
                with _job_lock:
                    _job.log.append(f"ERROR: {exc}")
            finally:
                with _job_lock:
                    _job.running = False
                    _job.current_playlist = None
                    _job.current_track = None

        threading.Thread(target=run_sync, daemon=True).start()
        return jsonify({"job_id": job_id})

    @app.get("/api/status")
    def api_status() -> Any:
        with _job_lock:
            return jsonify(
                {
                    "running": _job.running,
                    "current_playlist": _job.current_playlist,
                    "current_track": _job.current_track,
                    "done": _job.done,
                    "total": _job.total,
                    "downloaded": _job.downloaded,
                    "skipped": _job.skipped,
                    "failed": _job.failed,
                    "log": list(_job.log),
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
