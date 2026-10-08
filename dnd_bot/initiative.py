"""Initiative totals players report from Discord.

Players roll their own dice and tell the bot the total, or, when the portal is
showing them a battle, let the bot roll with their sheet's bonus. The bot only
holds the number until the DM applies it in the portal's tracker. Everything
here is pure so the slash command and the API share one set of rules.
"""

from __future__ import annotations

import re
import secrets
import time
import unicodedata
from collections.abc import Callable

VALUE_MIN = -20
VALUE_MAX = 60
NAME_MAX = 40
# Reports older than this are dropped: they belong to a fight that is long over.
MAX_AGE_HOURS = 12
# The most reports held at once. Enough for a big table plus their allies.
MAX_PENDING = 60
COOLDOWN_SECONDS = 2.0

_SPACE = re.compile(r"\s+")


def clean_name(raw: str) -> str:
    """A display name safe to store and show: no control characters, one line."""
    text = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C")
    return _SPACE.sub(" ", text).strip()[:NAME_MAX]


def valid_value(value: object) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and VALUE_MIN <= value <= VALUE_MAX
    )


class Cooldown:
    """One report per player every couple of seconds, so a held key cannot flood it."""

    def __init__(self, seconds: float = COOLDOWN_SECONDS, clock=time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._last: dict[str, float] = {}

    def ready(self, key: str) -> bool:
        now = self._clock()
        if len(self._last) > 512:
            self._last = {k: t for k, t in self._last.items() if now - t < self.seconds}
        if now - self._last.get(key, -1e9) < self.seconds:
            return False
        self._last[key] = now
        return True


MODES = ("normal", "advantage", "disadvantage")


def roll_initiative(
    bonus: int, mode: str = "normal", d20: Callable[[], int] | None = None
) -> tuple[int, str]:
    """d20 + bonus, with two dice for advantage or disadvantage. Returns (total, breakdown)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    roll = d20 or (lambda: secrets.randbelow(20) + 1)
    dice = [roll()] if mode == "normal" else [roll(), roll()]
    kept = max(dice) if mode == "advantage" else min(dice) if mode == "disadvantage" else dice[0]
    total = max(VALUE_MIN, min(VALUE_MAX, kept + bonus))
    shown = str(dice[0]) if len(dice) == 1 else f"{dice[0]}, {dice[1]} → {kept}"
    sign = "+" if bonus >= 0 else "-"
    return total, f"[{shown}] {sign} {abs(bonus)}"
