"""Tests for the YouTube download ledger (rolling 24h cap)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from djsync import downloads


@pytest.fixture
def download_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "downloads.json"
    monkeypatch.setattr(downloads, "LEDGER_PATH", path)
    return path


def test_remaining_today_counts_rolling_24h_window(
    download_ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(downloads, "DAILY_DOWNLOAD_CAP", 10)
    now = datetime(2024, 8, 1, 12, 0, 0, tzinfo=UTC)
    downloads.record_download(now=now - timedelta(hours=25))
    downloads.record_download(now=now - timedelta(hours=1))
    downloads.record_download(now=now)

    assert downloads.used_last_24h(now=now) == 2
    assert downloads.remaining_today(now=now) == 8
    assert downloads.can_download(8, now=now) is True
    assert downloads.can_download(9, now=now) is False
    assert downloads.can_download(0, now=now) is True
