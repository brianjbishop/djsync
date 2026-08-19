"""Tests for sigil parsing."""

from __future__ import annotations

import pytest

from djsync.sigils import has_sigil, parse_sigils


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("thug scott - $d", {"d"}),
        ("wicked, sexy! - $ave $d", {"ave", "d"}),
        ("\U0001F680 b.olt _ $log $d ", {"log", "d"}),
        ("šüñday - $track $ave $et $d ", {"track", "ave", "et", "d"}),
        ("André 3000", set()),
        ("Carnaval Retro", set()),
        ("7 _ 11 _ 7", set()),
        ("10 Vital.mp3", set()),
        ("RetroMix", set()),
        ("BILL$", set()),
        ("$5 bill", set()),
        ("a$ap rocky", set()),
        ("$D", {"d"}),
    ],
)
def test_parse_sigils(name: str, expected: set[str]) -> None:
    assert parse_sigils(name) == expected


def test_has_sigil() -> None:
    assert has_sigil("thug scott - $d", "d") is True
    assert has_sigil("thug scott - $d", "D") is True
    assert has_sigil("André 3000", "d") is False
