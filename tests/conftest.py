"""Test-wide isolation guards.

Every module that persists runtime state does so at a fixed path under the
project root: the fixture corpus, the Spotify quota ledger, the download
ledger, agent state, and the announced-events log. Those are real data - the
quota ledger in particular records live API lockouts.

A test that reads them is not testing anything reproducible, and a test that
writes them corrupts state the running system depends on. Both have already
happened here: synthetic match records were committed into the fixture corpus,
and a real quota lockout leaked into agent tests and silently changed their
behaviour.

Redirect all of it, for every test, regardless of what the test exercises.
"""

from __future__ import annotations

import pytest

from djsync import agent, downloads, events, quota


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DJSYNC_FIXTURES_DIR", str(tmp_path / "fixtures"))
    monkeypatch.setenv("DJSYNC_BEEPER_TOKEN", "")

    monkeypatch.setattr(quota, "LEDGER_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(agent, "STATE_PATH", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOCK_PATH", tmp_path / "agent.lock")
    monkeypatch.setattr(events, "EVENTS_PATH", tmp_path / "events.json")
    for attr in ("LEDGER_PATH", "DOWNLOADS_PATH", "STATE_PATH"):
        if hasattr(downloads, attr):
            monkeypatch.setattr(downloads, attr, tmp_path / f"downloads_{attr}.json")
