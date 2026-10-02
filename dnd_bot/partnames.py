"""Names for the parts of a game that was split across sessions."""

from __future__ import annotations

import re

MAX_NAME = 100
_PART_SUFFIX = re.compile(r"\s*\(part \d+\)\s*$", re.IGNORECASE)


def base_name(name: str) -> str:
    """`name` without one trailing " (part N)"."""
    return _PART_SUFFIX.sub("", name or "").strip()


def part_name(name: str, part: int) -> str:
    """`Name (part N)`, never stacking suffixes and never longer than MAX_NAME."""
    suffix = f" (part {part})"
    base = base_name(name) or "Session"
    return base[: MAX_NAME - len(suffix)].rstrip() + suffix
