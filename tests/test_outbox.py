"""Staging a finished session for collection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dnd_bot import paths
from dnd_bot.contract import READY_MARKER, is_marked, read_metadata
from dnd_bot.outbox import discard, metadata_from_session, pending, publish

SESSION = {
    "id": "s1",
    "name": "Kamp Gecesi",
    "start_time": "2026-08-29T18:00:00+00:00",
    "end_time": "2026-08-29T22:00:00+00:00",
    "channel_name": "Genel",
    "participants_json": '{"10": "Thorin", "11": "Elenya"}',
    "offsets_json": '{"10": 0.0, "11": 12.5}',
    "language": "tr",
}


def seed_audio(sessions_root: Path, session_id: str, users=("10", "11"), fmt="opus") -> None:
    paths.ensure_session_dirs(sessions_root, session_id)
    for user_id in users:
        paths.finalized_audio_path(sessions_root, session_id, user_id, fmt).write_bytes(
            b"audio" * 100
        )


def test_metadata_is_built_from_the_session_row():
    metadata = metadata_from_session(SESSION, timezone_name="Europe/Istanbul", prompt_extra="Volo")
    assert metadata.participants == {"10": "Thorin", "11": "Elenya"}
    assert metadata.offsets == {"10": 0.0, "11": 12.5}
    assert metadata.timezone == "Europe/Istanbul"
    assert metadata.prompt_extra == "Volo"
    assert metadata.channel_name == "Genel"


def test_publish_moves_audio_and_marks_ready(tmp_path: Path):
    sessions_root, outbox_root = tmp_path / "sessions", tmp_path / "outbox"
    seed_audio(sessions_root, "s1")

    target = publish(
        SESSION,
        sessions_root=sessions_root,
        outbox_root=outbox_root,
        audio_format="opus",
        timezone_name="Europe/Istanbul",
    )

    assert sorted(p.name for p in target.glob("*.opus")) == ["10.opus", "11.opus"]
    assert is_marked(target, READY_MARKER)
    assert read_metadata(target).name == "Kamp Gecesi"
    # Moved, not copied: the session directory no longer holds the tracks.
    assert not list(paths.audio_dir(sessions_root, "s1").glob("*.opus"))


def test_publish_can_copy_instead_of_moving(tmp_path: Path):
    sessions_root, outbox_root = tmp_path / "sessions", tmp_path / "outbox"
    seed_audio(sessions_root, "s1")

    publish(
        SESSION,
        sessions_root=sessions_root,
        outbox_root=outbox_root,
        audio_format="opus",
        timezone_name="UTC",
        move=False,
    )

    assert list(paths.audio_dir(sessions_root, "s1").glob("*.opus"))


def test_publish_is_idempotent(tmp_path: Path):
    sessions_root, outbox_root = tmp_path / "sessions", tmp_path / "outbox"
    seed_audio(sessions_root, "s1")
    first = publish(
        SESSION,
        sessions_root=sessions_root,
        outbox_root=outbox_root,
        audio_format="opus",
        timezone_name="UTC",
    )
    # A second call must not wipe the staged copy, even though the audio has moved.
    second = publish(
        SESSION,
        sessions_root=sessions_root,
        outbox_root=outbox_root,
        audio_format="opus",
        timezone_name="UTC",
    )
    assert first == second
    assert len(list(second.glob("*.opus"))) == 2


def test_publishing_without_audio_is_an_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="nothing to publish"):
        publish(
            SESSION,
            sessions_root=tmp_path / "sessions",
            outbox_root=tmp_path / "outbox",
            audio_format="opus",
            timezone_name="UTC",
        )


def test_pending_lists_only_completed_stagings(tmp_path: Path):
    sessions_root, outbox_root = tmp_path / "sessions", tmp_path / "outbox"
    seed_audio(sessions_root, "s1")
    publish(
        SESSION,
        sessions_root=sessions_root,
        outbox_root=outbox_root,
        audio_format="opus",
        timezone_name="UTC",
    )
    # A directory mid-copy, with no marker yet.
    (outbox_root / "s2").mkdir(parents=True)

    assert pending(outbox_root) == ["s1"]


def test_discard_removes_a_collected_session(tmp_path: Path):
    sessions_root, outbox_root = tmp_path / "sessions", tmp_path / "outbox"
    seed_audio(sessions_root, "s1")
    publish(
        SESSION,
        sessions_root=sessions_root,
        outbox_root=outbox_root,
        audio_format="opus",
        timezone_name="UTC",
    )

    freed = discard(outbox_root, "s1")

    assert freed > 0
    assert pending(outbox_root) == []
    assert discard(outbox_root, "s1") == 0  # already gone


def test_metadata_json_is_readable_by_a_plain_json_parser(tmp_path: Path):
    """The transcriber may be any language; keep the file boring."""
    sessions_root, outbox_root = tmp_path / "sessions", tmp_path / "outbox"
    seed_audio(sessions_root, "s1")
    target = publish(
        SESSION,
        sessions_root=sessions_root,
        outbox_root=outbox_root,
        audio_format="opus",
        timezone_name="UTC",
    )
    payload = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert payload["audio_format"] == "opus"
