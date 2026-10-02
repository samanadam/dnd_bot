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


def test_finalized_tracks_are_named_after_the_speaker(tmp_path: Path):
    sessions_root = tmp_path / "sessions"
    paths.ensure_session_dirs(sessions_root, "s1")
    paths.raw_pcm_path(sessions_root, "s1", "10").write_bytes(b"\x01\x00" * 4800)
    paths.raw_pcm_path(sessions_root, "s1", "11").write_bytes(b"\x01\x00" * 4800)

    written, _ = finalize_session_audio(sessions_root, "s1", "wav", labels={"10": "Thorin"})

    assert sorted(p.name for p in written) == ["11.wav", "Thorin_10.wav"]


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


def _frames(n: int, value: int = 1) -> bytes:
    return bytes([value, 0, value, 0]) * n  # n non-zero stereo frames


def test_pcm_to_wav_inserts_silence_at_gap_positions(tmp_path: Path):
    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(_frames(100, 1) + _frames(100, 2))
    wav = tmp_path / "10.wav"
    pcm_to_wav(pcm, wav, gaps=[(400, 4 * 50)])  # 50 silent frames after the first 100

    with wave.open(str(wav), "rb") as reader:
        assert reader.getnframes() == 250
        data = reader.readframes(250)
    assert data[:400] == _frames(100, 1)
    assert data[400:600] == bytes(200)
    assert data[600:] == _frames(100, 2)


def test_gap_at_a_read_chunk_boundary_lands_exactly(tmp_path: Path):
    one_mib = 1 << 20
    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(_frames(one_mib // 4 + 10))  # a bit more than one chunk
    wav = tmp_path / "10.wav"
    pcm_to_wav(pcm, wav, gaps=[(one_mib, 400)])

    with wave.open(str(wav), "rb") as reader:
        assert reader.getnframes() == one_mib // 4 + 10 + 100
        reader.setpos(one_mib // 4 - 1)
        assert reader.readframes(1) == _frames(1)
        assert reader.readframes(100) == bytes(400)
        assert reader.readframes(1) == _frames(1)


def test_gap_past_the_end_of_the_capture_goes_at_the_end(tmp_path: Path):
    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(_frames(10))
    wav = tmp_path / "10.wav"
    pcm_to_wav(pcm, wav, gaps=[(99_999_996, 40)])  # a crash lost the pcm tail

    with wave.open(str(wav), "rb") as reader:
        assert reader.getnframes() == 20


def test_finalize_applies_and_removes_the_gap_sidecar(tmp_path: Path):
    from dnd_bot.gaps import append_gap, gaps_path

    raw = paths.raw_dir(tmp_path, "s1")
    raw.mkdir(parents=True)
    (raw / "10.pcm").write_bytes(_frames(100))
    with gaps_path(raw / "10.pcm").open("ab") as handle:
        append_gap(handle, 200, BYTES_PER_SECOND)  # one second of silence mid-capture

    written, warnings = finalize_session_audio(tmp_path, "s1", "wav")

    assert warnings == []
    with wave.open(str(written[0]), "rb") as reader:
        assert reader.getnframes() == 100 + 48_000
    assert not (raw / "10.pcm").exists()
    assert not (raw / "10.gaps").exists()


def test_captured_seconds_counts_the_silence(tmp_path: Path):
    from dnd_bot.gaps import append_gap, gaps_path

    raw = paths.raw_dir(tmp_path, "s1")
    raw.mkdir(parents=True)
    (raw / "10.pcm").write_bytes(bytes(BYTES_PER_SECOND))
    with gaps_path(raw / "10.pcm").open("ab") as handle:
        append_gap(handle, 0, 2 * BYTES_PER_SECOND)

    assert captured_seconds(tmp_path, "s1") == 3.0


needs_ffmpeg = pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None, reason="ffmpeg is required to encode Opus"
)


def _ffprobe_seconds(path: Path) -> float:
    import subprocess

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def test_padded_stream_matches_what_the_wav_holds(tmp_path: Path):
    from dnd_bot.audio import iter_padded_pcm

    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(_frames(100, 1) + _frames(100, 2))
    streamed = b"".join(iter_padded_pcm(pcm, [(400, 200)]))
    assert streamed == _frames(100, 1) + bytes(200) + _frames(100, 2)


@needs_ffmpeg
def test_opus_track_is_as_long_as_the_session_and_leaves_no_temp_file(tmp_path: Path):
    from dnd_bot.audio import finalize_capture
    from dnd_bot.gaps import append_gap, gaps_path

    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(_frames(48_000))  # 1 s of speech
    with gaps_path(pcm).open("ab") as handle:
        append_gap(handle, 0, 9 * BYTES_PER_SECOND)  # 9 s of silence before it

    out = finalize_capture(pcm, tmp_path / "10.opus", "opus")

    assert _ffprobe_seconds(out) == pytest.approx(10.0, abs=0.1)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["10.gaps", "10.opus", "10.pcm"]


@needs_ffmpeg
def test_a_failing_encode_raises_and_leaves_no_half_file(tmp_path: Path, monkeypatch):
    from dnd_bot import audio

    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(_frames(48_000))
    monkeypatch.setitem(audio.ENCODER_ARGS, "opus", ["-c:a", "no_such_codec"])

    with pytest.raises(AudioError, match="ffmpeg failed encoding 10.pcm"):
        audio.finalize_capture(pcm, tmp_path / "10.opus", "opus")
    assert not (tmp_path / "10.opus").exists()


def test_an_empty_capture_is_still_refused_for_opus(tmp_path: Path):
    from dnd_bot.audio import finalize_capture

    pcm = tmp_path / "10.pcm"
    pcm.write_bytes(b"")
    with pytest.raises(AudioError, match="empty"):
        finalize_capture(pcm, tmp_path / "10.opus", "opus")
