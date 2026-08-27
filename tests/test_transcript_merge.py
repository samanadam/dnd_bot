"""Merging multiple speakers onto one timeline is the core correctness risk."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dnd_bot.transcription import RawSegment, merge_segments, render_json, render_markdown

TZ = ZoneInfo("Europe/Istanbul")
START = datetime(2026, 5, 1, 18, 0, 0, tzinfo=UTC)


def test_segments_are_ordered_by_absolute_session_time():
    per_user = {
        "10": [RawSegment(0.0, 2.0, "ilk"), RawSegment(10.0, 12.0, "sonra")],
        "11": [RawSegment(0.0, 1.0, "araya girdim")],
    }
    offsets = {"10": 0.0, "11": 5.0}
    labels = {"10": "Thorin", "11": "Elenya"}

    merged = merge_segments(per_user, offsets, labels)

    assert [(s.speaker, s.start) for s in merged] == [
        ("Thorin", 0.0),
        ("Elenya", 5.0),
        ("Thorin", 10.0),
    ]


def test_overlapping_speech_is_deterministically_ordered():
    per_user = {
        "10": [RawSegment(0.0, 4.0, "uzun konusma")],
        "11": [RawSegment(0.0, 1.0, "kisa")],
    }
    merged = merge_segments(per_user, {"10": 0.0, "11": 0.0}, {"10": "Thorin", "11": "Elenya"})

    # Same start: shorter segment first, then speaker name as the final tiebreak.
    assert [s.speaker for s in merged] == ["Elenya", "Thorin"]
    assert (
        merge_segments(
            {"11": per_user["11"], "10": per_user["10"]},
            {"10": 0.0, "11": 0.0},
            {"10": "Thorin", "11": "Elenya"},
        )
        == merged
    )


def test_offsets_shift_each_speaker_onto_the_session_timeline():
    merged = merge_segments(
        {"11": [RawSegment(3.0, 4.0, "gec geldim")]}, {"11": 12.5}, {"11": "Elenya"}
    )
    assert merged[0].start == 15.5
    assert merged[0].end == 16.5


def test_unknown_user_falls_back_to_a_readable_label():
    merged = merge_segments({"77": [RawSegment(0.0, 1.0, "kim")]}, {}, {})
    assert merged[0].speaker == "User 77"


def test_blank_segments_are_dropped():
    merged = merge_segments(
        {"10": [RawSegment(0.0, 1.0, "   "), RawSegment(1.0, 2.0, "gercek")]},
        {"10": 0.0},
        {"10": "Thorin"},
    )
    assert [s.text for s in merged] == ["gercek"]


def test_markdown_uses_local_wall_clock_timestamps():
    merged = merge_segments(
        {"10": [RawSegment(0.0, 1.0, "merhaba")]}, {"10": 0.0}, {"10": "Thorin"}
    )
    markdown = render_markdown(
        merged,
        session_name="Test Session",
        session_start=START,
        tz=TZ,
        duration_seconds=7200,
    )
    # 18:00 UTC is 21:00 in Europe/Istanbul.
    assert "[21:00:00] Thorin: merhaba" in markdown
    assert "**Duration:** 02:00:00" in markdown
    assert "**Speakers:** 1 (Thorin)" in markdown


def test_markdown_handles_a_silent_session():
    markdown = render_markdown(
        [], session_name="Quiet", session_start=START, tz=TZ, duration_seconds=60
    )
    assert "No speech was detected" in markdown


def test_warnings_are_surfaced_in_both_outputs():
    warnings = ["Skipped speaker 12: corrupt file"]
    markdown = render_markdown(
        [], session_name="S", session_start=START, tz=TZ, duration_seconds=1, warnings=warnings
    )
    payload = render_json(
        [],
        session_id="abc",
        session_name="S",
        session_start=START,
        tz=TZ,
        duration_seconds=1,
        language="tr",
        model="medium",
        warnings=warnings,
    )
    assert "corrupt file" in markdown
    assert payload["warnings"] == warnings


def test_json_payload_carries_structured_segments():
    merged = merge_segments({"10": [RawSegment(0.0, 1.5, "selam")]}, {"10": 2.0}, {"10": "Thorin"})
    payload = render_json(
        merged,
        session_id="abc",
        session_name="S",
        session_start=START,
        tz=TZ,
        duration_seconds=10,
        language="tr",
        model="medium",
    )
    segment = payload["segments"][0]
    assert segment["speaker"] == "Thorin"
    assert segment["user_id"] == "10"
    assert segment["start"] == 2.0
    assert segment["end"] == 3.5
    assert segment["start_local"].startswith("2026-05-01T21:00:02")
    assert payload["word_count"] == 1
