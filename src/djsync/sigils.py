"""Parse sigils from playlist names.

A sigil is ``$`` immediately followed by one or more ASCII letters, appearing
as its own token (preceded by start-of-string or whitespace, followed by
whitespace or end-of-string).
"""

from __future__ import annotations

import re

_SIGIL_PATTERN = re.compile(r"(?:^|\s)\$([A-Za-z]+)(?=\s|$)")


def parse_sigils(name: str) -> set[str]:
    """Return lowercase sigil names (without ``$``) found in *name*."""
    return {match.lower() for match in _SIGIL_PATTERN.findall(name)}


def has_sigil(name: str, sigil: str) -> bool:
    """Return whether *name* contains the given sigil (case-insensitive)."""
    return sigil.lower() in parse_sigils(name)
