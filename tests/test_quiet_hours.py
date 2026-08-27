"""Quiet-hours window math, including the wrap-past-midnight case."""

from __future__ import annotations

from datetime import datetime
from datetime import time as dtime

from dnd_bot.quiet_hours import in_quiet_hours, seconds_until_window

MIDNIGHT = dtime(0, 0)
EIGHT_AM = dtime(8, 0)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 1, hour, minute)


def test_inside_a_simple_window():
    assert in_quiet_hours(at(2), MIDNIGHT, EIGHT_AM) is True
    assert in_quiet_hours(at(0), MIDNIGHT, EIGHT_AM) is True


def test_outside_a_simple_window():
    assert in_quiet_hours(at(9), MIDNIGHT, EIGHT_AM) is False
    assert in_quiet_hours(at(8), MIDNIGHT, EIGHT_AM) is False  # end is exclusive


def test_window_wrapping_past_midnight():
    start, end = dtime(23, 0), dtime(6, 0)
    assert in_quiet_hours(at(23, 30), start, end) is True
    assert in_quiet_hours(at(1), start, end) is True
    assert in_quiet_hours(at(12), start, end) is False


def test_disabled_means_always_open():
    assert in_quiet_hours(at(12), MIDNIGHT, EIGHT_AM, enabled=False) is True


def test_equal_start_and_end_means_always_open():
    assert in_quiet_hours(at(12), dtime(3, 0), dtime(3, 0)) is True


def test_wait_is_zero_inside_the_window():
    assert seconds_until_window(at(2), MIDNIGHT, EIGHT_AM) == 0.0


def test_wait_counts_down_to_the_next_opening():
    # 09:00, window opens at 23:00 the same day -> 14 hours.
    assert seconds_until_window(at(9), dtime(23, 0), dtime(6, 0)) == 14 * 3600


def test_wait_rolls_over_to_tomorrow():
    # 09:00, window opens at 00:00 -> 15 hours until midnight.
    assert seconds_until_window(at(9), MIDNIGHT, EIGHT_AM) == 15 * 3600
