"""Tests for web API destination endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from djsync.config import Destination
from djsync.web import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DJSYNC_DRIVE", str(tmp_path / "drive"))
    dest = Destination(
        drive=tmp_path / "drive",
        library_root="dj",
        collection="playlists",
    )
    with patch("djsync.web.get_destination", return_value=dest):
        app = create_app()
        app.config["TESTING"] = True
        with app.test_client() as test_client:
            yield test_client, dest


def test_api_destination(client) -> None:
    test_client, dest = client
    resp = test_client.get("/api/destination")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["path"] == str(dest.path)
    assert data["mounted"] is False
    assert data["free_bytes"] is None
    assert data["free_human"] is None


def test_index_no_store_cache_control(client) -> None:
    test_client, _dest = client
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"


def test_api_sync_rejects_unmounted_drive(client) -> None:
    test_client, _dest = client
    resp = test_client.post(
        "/api/sync",
        json={"playlist_ids": ["abc"]},
    )
    assert resp.status_code == 409
    assert "not connected" in resp.get_json()["error"].lower()
