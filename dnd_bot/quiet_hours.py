"""Quiet-hours window math for the deferred transcription queue."""

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dtime


def in_quiet_hours(now: datetime, start: dtime, end: dtime, enabled: bool = True) -> bool:
    """True when `now` falls inside the configured window.

    A window where start == end is treated as "always open" (24h), and a window
    that wraps past midnight (e.g. 23:00 -> 08:00) is handled explicitly.
    """
    if not enabled:
        return True
    current = now.time()
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def seconds_until_window(now: datetime, start: dtime, end: dtime, enabled: bool = True) -> float:
    """Seconds to wait before the window opens. 0 when it is already open."""
    if in_quiet_hours(now, start, end, enabled):
        return 0.0
    next_start = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if next_start <= now:
        next_start += timedelta(days=1)
    return max(0.0, (next_start - now).total_seconds())
