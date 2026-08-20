"""Test-wide guards.

The fixture corpus under fixtures/ is real data - it is what makes offline
matcher replay possible. Tests must never write into it, so redirect the
fixtures directory for every test regardless of what it exercises.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_fixture_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("DJSYNC_FIXTURES_DIR", str(tmp_path / "fixtures"))
