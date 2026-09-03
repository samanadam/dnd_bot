"""Refusing a session that cannot fit on the disk."""

from __future__ import annotations

import pytest

from dnd_bot.audio import BYTES_PER_SECOND
from dnd_bot.capacity import (
    Estimate,
    estimate,
    required_bytes,
    shortfall_message,
    warning_message,
)

GB = 1_000_000_000


def test_one_speaker_hour_is_the_documented_rate():
    """0.7 GB per speaker-hour is the number the README budgets with."""
    one_hour = required_bytes(speakers=1, hours=1)
    # One capture plus the intermediate WAV allowance.
    assert one_hour == 2 * 3600 * BYTES_PER_SECOND
    assert 1.35 * GB < one_hour < 1.45 * GB


def test_six_speakers_six_hours_matches_the_hand_calculation():
    """The case that motivated this: ~29 GB peak."""
    peak = required_bytes(speakers=6, hours=6)
    assert 28 * GB < peak < 30 * GB


def test_more_speakers_needs_more_space():
    assert required_bytes(6, 4) > required_bytes(3, 4)


def test_a_channel_with_nobody_still_budgets_for_one_speaker():
    assert required_bytes(0, 4) == required_bytes(1, 4)


def test_negative_hours_cannot_produce_a_negative_requirement():
    assert required_bytes(4, -3) == 0


def test_estimate_reads_free_space_from_the_filesystem(tmp_path):
    est = estimate(tmp_path, speakers=2, hours=1)
    assert est.speakers == 2
    assert est.free_bytes > 0
    assert est.required_bytes == required_bytes(2, 1)


# -- verdicts ---------------------------------------------------------------


def make(required: int, free: int) -> Estimate:
    return Estimate(speakers=5, hours=4, required_bytes=required, free_bytes=free)


def test_plenty_of_room_is_sufficient_and_comfortable():
    est = make(required=10 * GB, free=100 * GB)
    assert est.sufficient
    assert est.comfortable


def test_just_over_the_requirement_is_allowed_but_not_comfortable():
    est = make(required=10 * GB, free=11 * GB)
    assert est.sufficient
    assert not est.comfortable


def test_under_the_requirement_is_refused():
    est = make(required=10 * GB, free=9 * GB)
    assert not est.sufficient


def test_exactly_the_requirement_is_allowed():
    est = make(required=10 * GB, free=10 * GB)
    assert est.sufficient


@pytest.mark.parametrize("message", [shortfall_message, warning_message])
def test_messages_quote_both_numbers(message):
    text = message(make(required=29 * GB, free=3 * GB))
    assert "29.0 GB" in text
    assert "3.0 GB" in text
