from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dnd_bot.ids import is_dated, new_session_id, short_id

ISTANBUL = ZoneInfo("Europe/Istanbul")


def test_id_leads_with_the_local_date_and_time():
    # 21:30 UTC is 00:30 the next day in Istanbul: the id must show the local
    # day, since that is the one the players would name the session after.
    started = datetime(2026, 9, 9, 21, 30, tzinfo=UTC)
    session_id = new_session_id(started, ISTANBUL)
    assert session_id.startswith("2026-09-10-0030-")
    assert is_dated(session_id)


def test_ids_are_unique_within_the_same_minute():
    started = datetime(2026, 9, 9, 21, 30, tzinfo=UTC)
    ids = {new_session_id(started, ISTANBUL) for _ in range(200)}
    assert len(ids) == 200


def test_ids_sort_chronologically():
    tz = ISTANBUL
    early = new_session_id(datetime(2026, 9, 9, 18, 0, tzinfo=UTC), tz)
    late = new_session_id(datetime(2026, 9, 9, 20, 0, tzinfo=UTC), tz)
    next_day = new_session_id(datetime(2026, 9, 10, 8, 0, tzinfo=UTC), tz)
    assert sorted([next_day, late, early]) == [early, late, next_day]


def test_id_is_a_single_path_segment():
    session_id = new_session_id(datetime(2026, 9, 9, 21, 30, tzinfo=UTC), ISTANBUL)
    assert "/" not in session_id and "\\" not in session_id


def test_short_id_of_a_dated_id_is_the_random_tail():
    assert short_id("2026-09-09-2130-a1b2c3d4") == "a1b2c3d4"


def test_short_id_still_handles_ids_minted_before_the_change():
    legacy = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    assert not is_dated(legacy)
    assert short_id(legacy) == "3f2504e0"
