"""Every module must import standalone.

A circular import (report -> agent -> beeper -> report) shipped undetected
because the suite only ever imported these modules in an order that happened
to work. Importing each one first, in a fresh interpreter, is the only way to
catch that.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")

MODULES = [
    "agent", "beeper", "beeper_auth", "cache", "cli", "config", "downloads",
    "events", "fixtures", "library", "models", "quota", "report", "selection",
    "sigils", "spotify", "sync", "tagging", "web", "web_helpers",
    "matcher.candidate", "matcher.score", "matcher.search",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_standalone(module: str) -> None:
    # uv's editable .pth is unreliable in this venv, so point the child at
    # src explicitly rather than relying on installation.
    env = {**os.environ, "PYTHONPATH": SRC}
    proc = subprocess.run(
        [sys.executable, "-c", f"import djsync.{module}"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"import djsync.{module} failed:\n{proc.stderr}"
