"""Response serializers are an allowlist, not a dump of internal rows.

The API is reachable from the public internet behind one shared token. A
session row carries `started_by_user_id`, `participants_json` and
`offsets_json`, all of which hold real Discord user ids. Nothing in this
package may put those on the wire, so every response is built field by field
here and the leak tests below assert the ids never make it out.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from dnd_bot.api import schemas

# The ids the `session_row` fixture uses for its two speakers. Leak tests look
# for these exact values anywhere in a serialized response.
SPEAKER_IDS = ("10", "11")


def all_strings(value) -> list[str]:
    """Every string reachable in a nested response, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            out.append(key)
            out.extend(all_strings(item))
        return out
    if isinstance(value, list | tuple):
        out = []
        for item in value:
            out.extend(all_strings(item))
        return out
    return []


def fake_active_session(**overrides):
    defaults = {
        "session_id": "session-2",
        "name": "Live Game",
        "channel_id": 2,
        "channel_name": "Table",
        "started_by_user_id": "10",
        "labels": {"10": "Thorin", "11": "Elenya"},
        "offsets": {"10": 0.0, "11": 12.5},
        "warnings": [],
        "elapsed_seconds": lambda: 125.0,
    }
    defaults.update(overrides)
    start = defaults.pop("start_time", None)
    return SimpleNamespace(start_time=start, **defaults)


# -- session summaries -------------------------------------------------------


def test_session_summary_exposes_only_allowlisted_fields(session_row):
    out = schemas.session_summary(session_row)
    assert set(out) == {
        "id",
        "name",
        "channel_name",
        "started_at",
        "ended_at",
        "duration_seconds",
        "transcribed",
        "cancelled",
        "speakers",
        "speaker_count",
        "campaign_id",
        "campaign_name",
    }


def test_campaign_exposes_only_allowlisted_fields():
    row = {
        "id": "abc123abc123",
        "name": "Strahd",
        "channel_id": "7",
        "language": None,
        "archived": 0,
        "session_count": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    assert set(schemas.campaign(row)) == {
        "id",
        "name",
        "channel_id",
        "language",
        "archived",
        "session_count",
    }


def test_session_summary_derives_duration_from_the_timestamps(session_row):
    # The fixture runs 18:00 -> 20:00 UTC. Duration is not a stored column.
    assert schemas.session_summary(session_row)["duration_seconds"] == 7200.0


def test_session_summary_reports_labels_and_a_count_not_ids(session_row):
    out = schemas.session_summary(session_row)
    assert out["speakers"] == ["Elenya", "Thorin"]
    assert out["speaker_count"] == 2


def test_session_summary_never_leaks_discord_user_ids(session_row):
    strings = all_strings(schemas.session_summary(session_row))
    assert not set(strings) & set(SPEAKER_IDS)
    assert "started_by_user_id" not in strings
    assert "participants_json" not in strings
    assert "offsets_json" not in strings
    assert "guild_id" not in strings


def test_session_summary_survives_a_row_with_no_participants(session_row):
    session_row["participants_json"] = None
    session_row["end_time"] = None
    out = schemas.session_summary(session_row)
    assert out["speakers"] == []
    assert out["speaker_count"] == 0
    assert out["duration_seconds"] is None


# -- live sessions -----------------------------------------------------------


def test_active_session_reports_elapsed_time_and_the_channel(session_row):
    out = schemas.active_session(fake_active_session())
    assert out["session_id"] == "session-2"
    assert out["elapsed_seconds"] == 125.0
    # The portal needs the channel id back to address /recording/stop.
    assert out["channel_id"] == "2"
    assert out["channel_name"] == "Table"


def test_active_session_never_leaks_discord_user_ids():
    strings = all_strings(schemas.active_session(fake_active_session()))
    assert not set(strings) & set(SPEAKER_IDS)
    assert "started_by_user_id" not in strings
    assert "offsets" not in strings


def test_active_session_passes_warnings_through():
    session = fake_active_session(warnings=["Reconnected once"])
    assert schemas.active_session(session)["warnings"] == ["Reconnected once"]


# -- health and stats --------------------------------------------------------


def test_health_is_serializable_and_says_nothing_it_need_not():
    """No version: this is the one response served without a token."""
    out = schemas.health(ready=True, uptime_seconds=61.4, db_ok=True)
    assert out["status"] == "ok"
    assert out["ready"] is True
    assert out["uptime_seconds"] == 61
    assert out["db"] == "ok"
    assert "version" not in out
    json.dumps(out)


def test_health_reports_degraded_when_the_database_is_unreachable():
    out = schemas.health(ready=True, uptime_seconds=1.0, db_ok=False)
    assert out["status"] == "degraded"
    assert out["db"] == "unreachable"


def test_stats_nests_sessions_disk_and_storage(session_row):
    out = schemas.stats(
        active=[schemas.active_session(fake_active_session())],
        pending_transcription=2,
        free_mb=1234.5,
        data_bytes=987,
        low_disk=False,
        storage_backend="r2",
        storage_reachable=True,
        music=None,
    )
    assert out["sessions"]["pending_transcription"] == 2
    assert len(out["sessions"]["active"]) == 1
    assert out["disk"] == {"free_mb": 1234.5, "data_bytes": 987, "low_disk": False}
    assert out["storage"] == {"backend": "r2", "reachable": True}
    assert out["version"]
    assert out["music"] is None
    json.dumps(out)


def test_stats_never_leaks_a_filesystem_path():
    out = schemas.stats(
        active=[],
        pending_transcription=0,
        free_mb=1.0,
        data_bytes=0,
        low_disk=True,
        storage_backend="local",
        storage_reachable=None,
        music=None,
    )
    for text in all_strings(out):
        assert "/" not in text or text in {"local"}


# -- errors ------------------------------------------------------------------


def test_error_wraps_a_code_and_message():
    assert schemas.error("conflict", "Already recording") == {
        "error": {"code": "conflict", "message": "Already recording"}
    }
