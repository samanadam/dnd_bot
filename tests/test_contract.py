"""The recorder/transcriber interface. Both repos must agree on this exactly."""

from __future__ import annotations

from pathlib import Path

import pytest

from dnd_bot.contract import (
    DONE_MARKER,
    READY_MARKER,
    SCHEMA_VERSION,
    ContractError,
    SessionMetadata,
    audio_tracks,
    done_sessions,
    is_marked,
    mark,
    read_metadata,
    ready_sessions,
    validate_outbox,
    write_metadata,
)

SAMPLE = SessionMetadata(
    session_id="s1",
    name="Kamp Gecesi",
    start_time_utc="2026-08-29T18:00:00+00:00",
    end_time_utc="2026-08-29T22:00:00+00:00",
    timezone="Europe/Istanbul",
    channel_name="Genel",
    participants={"10": "Thorin", "11": "Elenya"},
    offsets={"10": 0.0, "11": 12.5},
    language="tr",
    prompt_extra="Neverwinter",
)


def stage_outbox(root: Path, *, marker: bool = True, tracks=("10", "11")) -> Path:
    directory = root / SAMPLE.session_id
    directory.mkdir(parents=True, exist_ok=True)
    for user_id in tracks:
        (directory / f"{user_id}.opus").write_bytes(b"fake opus")
    write_metadata(directory, SAMPLE)
    if marker:
        mark(directory, READY_MARKER)
    return directory


def test_metadata_survives_a_round_trip(tmp_path: Path):
    write_metadata(tmp_path, SAMPLE)
    assert read_metadata(tmp_path) == SAMPLE


def test_non_ascii_names_survive(tmp_path: Path):
    metadata = SessionMetadata(
        session_id="s2",
        name="Ejderha Çukuru",
        start_time_utc="2026-08-29T18:00:00+00:00",
        participants={"10": "Şölen"},
    )
    write_metadata(tmp_path, metadata)
    assert read_metadata(tmp_path).participants == {"10": "Şölen"}


def test_offsets_come_back_as_floats(tmp_path: Path):
    write_metadata(tmp_path, SAMPLE)
    offsets = read_metadata(tmp_path).offsets
    assert offsets["11"] == 12.5
    assert all(isinstance(value, float) for value in offsets.values())


def test_a_future_schema_is_refused_loudly(tmp_path: Path):
    """Two repos drifting apart must fail, not silently misread."""
    (tmp_path / "metadata.json").write_text(
        '{"schema": 999, "session_id": "s", "name": "n", "start_time_utc": "t"}',
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="Unsupported metadata schema"):
        read_metadata(tmp_path)


def test_missing_required_fields_are_reported(tmp_path: Path):
    (tmp_path / "metadata.json").write_text(
        f'{{"schema": {SCHEMA_VERSION}, "name": "n"}}', encoding="utf-8"
    )
    with pytest.raises(ContractError, match="missing required field"):
        read_metadata(tmp_path)


def test_corrupt_json_is_reported(tmp_path: Path):
    (tmp_path / "metadata.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError, match="not valid JSON"):
        read_metadata(tmp_path)


def test_missing_metadata_is_reported(tmp_path: Path):
    with pytest.raises(ContractError, match="No metadata.json"):
        read_metadata(tmp_path)


def test_only_marked_directories_are_listed(tmp_path: Path):
    stage_outbox(tmp_path)
    half_copied = tmp_path / "s2"
    half_copied.mkdir()
    (half_copied / "10.opus").write_bytes(b"still copying")

    assert [p.name for p in ready_sessions(tmp_path)] == ["s1"]


def test_ready_listing_is_empty_without_a_root(tmp_path: Path):
    assert ready_sessions(tmp_path / "nope") == []
    assert done_sessions(tmp_path / "nope") == []


def test_done_marker_listing(tmp_path: Path):
    directory = tmp_path / "s1"
    directory.mkdir()
    assert done_sessions(tmp_path) == []
    mark(directory, DONE_MARKER)
    assert [p.name for p in done_sessions(tmp_path)] == ["s1"]
    assert is_marked(directory, DONE_MARKER)


def test_audio_tracks_are_found_by_format(tmp_path: Path):
    directory = stage_outbox(tmp_path)
    assert [p.stem for p in audio_tracks(directory, "opus")] == ["10", "11"]
    assert audio_tracks(directory, "wav") == []


def test_validate_accepts_a_complete_outbox(tmp_path: Path):
    directory = stage_outbox(tmp_path)
    assert validate_outbox(directory).session_id == "s1"


def test_validate_rejects_an_unmarked_directory(tmp_path: Path):
    directory = stage_outbox(tmp_path, marker=False)
    with pytest.raises(ContractError, match="no READY marker"):
        validate_outbox(directory)


def test_validate_rejects_a_session_with_no_audio(tmp_path: Path):
    directory = stage_outbox(tmp_path, tracks=())
    with pytest.raises(ContractError, match="no .opus tracks"):
        validate_outbox(directory)


def test_an_unlabelled_speaker_gets_a_placeholder_rather_than_a_failure(tmp_path: Path):
    """A speaker who joined late should not cost everyone else their transcript."""
    directory = stage_outbox(tmp_path, tracks=("10", "11", "99"))
    metadata = validate_outbox(directory)
    assert metadata.participants["99"] == "User 99"
    assert metadata.participants["10"] == "Thorin"
