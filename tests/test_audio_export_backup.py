"""Finalization, export bundling and backup pruning."""

from __future__ import annotations

import wave
from datetime import date
from pathlib import Path

import pytest

from dnd_bot import paths
from dnd_bot.audio import BYTES_PER_SECOND, AudioError, pcm_duration_seconds, pcm_to_wav
from dnd_bot.backup import backup_database, backup_filename, prune_targets
from dnd_bot.db import Database
from dnd_bot.exports import ExportError, build_export, fits_discord_upload
from dnd_bot.finalize import captured_seconds, finalize_session_audio

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def test_pcm_duration_matches_the_discord_format():
    assert pcm_duration_seconds(BYTES_PER_SECOND) == 1.0


def test_pcm_is_wrapped_into_a_readable_wav(tmp_path: Path):
    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(b"\x01\x00" * 4800)
    wav = tmp_path / "10.wav"

    pcm_to_wav(pcm, wav)

    with wave.open(str(wav), "rb") as reader:
        assert reader.getnchannels() == 2
        assert reader.getframerate() == 48_000
        assert reader.getsampwidth() == 2
        assert reader.getnframes() == 2400


def test_a_truncated_final_frame_still_produces_a_valid_wav(tmp_path: Path):
    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(b"\x01\x00" * 100 + b"\x07")  # crash mid-frame
    wav = tmp_path / "10.wav"

    pcm_to_wav(pcm, wav)

    with wave.open(str(wav), "rb") as reader:
        assert reader.getnframes() == 50


def test_empty_capture_is_rejected(tmp_path: Path):
    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(b"")
    with pytest.raises(AudioError):
        pcm_to_wav(pcm, tmp_path / "10.wav")


def test_one_broken_speaker_does_not_block_the_others(tmp_path: Path):
    sessions_root = tmp_path / "sessions"
    paths.ensure_session_dirs(sessions_root, "s1")
    paths.raw_pcm_path(sessions_root, "s1", "10").write_bytes(b"\x01\x00" * 4800)
    paths.raw_pcm_path(sessions_root, "s1", "11").write_bytes(b"")  # broken

    written, warnings = finalize_session_audio(sessions_root, "s1", "wav")

    assert [p.stem for p in written] == ["10"]
    assert len(warnings) == 1 and "11" in warnings[0]
    # The good speaker's raw capture is consumed, the broken one is left for triage.
    assert not paths.raw_pcm_path(sessions_root, "s1", "10").exists()
    assert paths.raw_pcm_path(sessions_root, "s1", "11").exists()


def test_captured_seconds_reports_the_longest_speaker(tmp_path: Path):
    sessions_root = tmp_path / "sessions"
    paths.ensure_session_dirs(sessions_root, "s1")
    paths.raw_pcm_path(sessions_root, "s1", "10").write_bytes(b"\x00" * BYTES_PER_SECOND)
    paths.raw_pcm_path(sessions_root, "s1", "11").write_bytes(b"\x00" * (BYTES_PER_SECOND * 3))
    assert captured_seconds(sessions_root, "s1") == 3.0


def test_export_bundles_audio_and_transcript(tmp_path: Path):
    import zipfile

    sessions_root = tmp_path / "sessions"
    exports_root = tmp_path / "exports"
    paths.ensure_session_dirs(sessions_root, "s1")
    paths.finalized_audio_path(sessions_root, "s1", "10").write_bytes(b"wavdata")
    paths.transcript_md_path(sessions_root, "s1").write_text("# transcript", encoding="utf-8")

    archive = build_export(sessions_root, exports_root, "s1")

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "transcript.md" in names
    assert any(name.endswith("10.wav") for name in names)
    assert fits_discord_upload(archive, 25) is True


def test_export_of_an_unknown_session_is_rejected(tmp_path: Path):
    with pytest.raises(ExportError):
        build_export(tmp_path / "sessions", tmp_path / "exports", "nope")


def test_oversized_export_is_flagged(tmp_path: Path):
    archive = tmp_path / "big.zip"
    archive.write_bytes(b"\x00" * 2_000_000)
    assert fits_discord_upload(archive, 1) is False


def test_prune_keeps_recent_backups_only():
    names = [
        backup_filename(date(2026, 5, 1)),
        backup_filename(date(2026, 4, 1)),
        "not-a-backup.txt",
    ]
    stale = prune_targets(names, keep_days=14, today=date(2026, 5, 10))
    assert stale == [backup_filename(date(2026, 4, 1))]


async def test_backup_produces_a_readable_copy(tmp_path: Path):
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    try:
        await db.set_character(10, "Thorin")
        target = backup_database(db.path, tmp_path / "backups", date(2026, 5, 10))
    finally:
        await db.close()

    copy = Database(target, MIGRATIONS)
    await copy.connect()
    try:
        assert await copy.character_map() == {"10": "Thorin"}
    finally:
        await copy.close()


def test_export_excludes_temporary_chunk_files(tmp_path: Path):
    """Chunks are duplicates of the tracks; shipping them would double the zip."""
    import zipfile

    from dnd_bot.chunking import CHUNK_DIR_NAME

    sessions_root = tmp_path / "sessions"
    paths.ensure_session_dirs(sessions_root, "s1")
    paths.finalized_audio_path(sessions_root, "s1", "10").write_bytes(b"wavdata")
    chunk_dir = paths.audio_dir(sessions_root, "s1") / CHUNK_DIR_NAME
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "10.part000.wav").write_bytes(b"duplicate")

    archive = build_export(sessions_root, tmp_path / "exports", "s1")

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert not any(CHUNK_DIR_NAME in name for name in names)
    assert any(name.endswith("10.wav") for name in names)
