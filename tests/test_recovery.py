"""Only genuinely incomplete sessions with audio still on disk are recoverable."""

from __future__ import annotations

from pathlib import Path

from dnd_bot import paths
from dnd_bot.finalize import has_raw_audio
from dnd_bot.recovery import find_recoverable


def row(session_id: str, *, completed=0, cancelled=0) -> dict:
    return {"id": session_id, "completed": completed, "cancelled": cancelled, "name": session_id}


def test_only_incomplete_sessions_with_audio_are_flagged():
    rows = [
        row("finished", completed=1),
        row("crashed"),
        row("crashed-no-audio"),
        row("cancelled", cancelled=1),
    ]
    with_audio = {"crashed", "cancelled", "finished"}

    recoverable = find_recoverable(rows, lambda sid: sid in with_audio)

    assert [r["id"] for r in recoverable] == ["crashed"]


def test_empty_input_is_handled():
    assert find_recoverable([], lambda _sid: True) == []


def test_has_raw_audio_ignores_zero_byte_captures(tmp_path: Path):
    sessions_root = tmp_path / "sessions"
    paths.ensure_session_dirs(sessions_root, "s1")
    empty = paths.raw_pcm_path(sessions_root, "s1", "10")
    empty.write_bytes(b"")
    assert has_raw_audio(sessions_root, "s1") is False

    empty.write_bytes(b"\x00\x01" * 100)
    assert has_raw_audio(sessions_root, "s1") is True


def test_has_raw_audio_false_for_unknown_session(tmp_path: Path):
    assert has_raw_audio(tmp_path, "nope") is False
