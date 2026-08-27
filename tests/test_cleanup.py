"""Retention deletes expired audio only - never transcripts, never exports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from dnd_bot import paths
from dnd_bot.cleanup import delete_session_audio, expired_sessions
from dnd_bot.timeutil import to_iso

NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


def test_only_expired_rows_are_selected():
    rows = [
        {"id": "expired", "audio_expires_at": to_iso(NOW - timedelta(seconds=1))},
        {"id": "due-now", "audio_expires_at": to_iso(NOW)},
        {"id": "future", "audio_expires_at": to_iso(NOW + timedelta(days=1))},
        {"id": "never", "audio_expires_at": None},
    ]
    assert [r["id"] for r in expired_sessions(rows, NOW)] == ["expired", "due-now"]


def test_delete_removes_audio_but_keeps_transcripts(tmp_path: Path):
    sessions_root = tmp_path / "sessions"
    paths.ensure_session_dirs(sessions_root, "s1")
    paths.raw_pcm_path(sessions_root, "s1", "10").write_bytes(b"\x00" * 1024)
    paths.finalized_audio_path(sessions_root, "s1", "10").write_bytes(b"\x00" * 2048)
    transcript = paths.transcript_md_path(sessions_root, "s1")
    transcript.write_text("# keep me", encoding="utf-8")

    freed = delete_session_audio(sessions_root, "s1")

    assert freed == 1024 + 2048
    assert not paths.audio_dir(sessions_root, "s1").exists()
    assert transcript.read_text(encoding="utf-8") == "# keep me"


def test_exports_are_untouched_by_cleanup(tmp_path: Path):
    sessions_root = tmp_path / "sessions"
    exports_root = tmp_path / "exports"
    exports_root.mkdir(parents=True)
    export = paths.export_path(exports_root, "s1")
    export.write_bytes(b"zipdata")
    paths.ensure_session_dirs(sessions_root, "s1")
    paths.raw_pcm_path(sessions_root, "s1", "10").write_bytes(b"\x00" * 10)

    delete_session_audio(sessions_root, "s1")

    assert export.exists()


def test_delete_is_a_noop_for_a_session_with_no_audio(tmp_path: Path):
    assert delete_session_audio(tmp_path, "missing") == 0


def test_orphaned_chunk_directories_are_purged(tmp_path: Path):
    """A crash mid-transcription leaves duplicate audio behind; it must not linger."""
    from dnd_bot.chunking import CHUNK_DIR_NAME
    from dnd_bot.cleanup import purge_orphaned_chunks

    sessions_root = tmp_path / "sessions"
    paths.ensure_session_dirs(sessions_root, "s1")
    chunk_dir = paths.audio_dir(sessions_root, "s1") / CHUNK_DIR_NAME
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "10.part000.wav").write_bytes(b"\x00" * 512)
    track = paths.finalized_audio_path(sessions_root, "s1", "10")
    track.write_bytes(b"\x00" * 128)

    freed = purge_orphaned_chunks(sessions_root)

    assert freed == 512
    assert not chunk_dir.exists()
    assert track.exists()  # the real track is untouched


def test_purge_is_a_noop_without_chunks(tmp_path: Path):
    from dnd_bot.cleanup import purge_orphaned_chunks

    assert purge_orphaned_chunks(tmp_path / "nope") == 0
