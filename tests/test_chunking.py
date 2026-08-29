"""Chunking is what keeps peak memory flat on a long session.

Everything here goes through ffmpeg, so the tests need it too. They generate
real audio rather than fixtures, and cover Opus as well as WAV since the
recorder now ships Opus.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from dnd_bot.chunking import (
    ANALYSIS_RATE,
    Chunk,
    ChunkingError,
    choose_boundary,
    cleanup_chunks,
    extract_chunk,
    plan_boundaries,
    probe_duration,
    rms_profile,
    split_audio,
)
from dnd_bot.jobs import transcribe_one
from dnd_bot.transcription import RawSegment

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def make_audio(path: Path, pattern: list[tuple[float, bool]]) -> Path:
    """Build a track alternating tone and silence, via ffmpeg.

    pattern: [(seconds, is_loud), ...]
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    filters = []
    for index, (seconds, loud) in enumerate(pattern):
        source = "sine=frequency=220" if loud else "anullsrc=r=48000:cl=stereo"
        parts += [
            "-f",
            "lavfi",
            "-t",
            f"{seconds}",
            "-i",
            f"{source}:sample_rate=48000" if loud else f"{source}",
        ]
        filters.append(f"[{index}:a]")
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        *parts,
        "-filter_complex",
        f"{''.join(filters)}concat=n={len(pattern)}:v=0:a=1[out]",
        "-map",
        "[out]",
        "-ac",
        "2",
        "-ar",
        "48000",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:  # pragma: no cover - surfaces a broken test env
        raise RuntimeError(f"ffmpeg fixture generation failed: {result.stderr}")
    return path


# -- pure boundary logic ---------------------------------------------------


def test_boundary_snaps_to_the_quietest_nearby_window():
    rms = np.array([9.0, 9.0, 9.0, 0.5, 9.0, 9.0, 9.0], dtype=np.float32)
    assert choose_boundary(rms, target_index=5, search_windows=3) == 3


def test_boundary_stays_put_when_no_quiet_spot_is_near():
    rms = np.array([9.0] * 10, dtype=np.float32)
    assert choose_boundary(rms, target_index=5, search_windows=2) == 3  # first of equal minima


def test_no_boundaries_for_a_file_shorter_than_the_target():
    rms = np.array([1.0] * 50, dtype=np.float32)
    assert plan_boundaries(rms, total_windows=50, target_windows=100, search_windows=5) == []


def test_boundaries_are_strictly_increasing_and_inside_the_file():
    rms = np.random.default_rng(0).random(1000).astype(np.float32)
    boundaries = plan_boundaries(rms, total_windows=1000, target_windows=100, search_windows=20)
    assert boundaries == sorted(boundaries)
    assert len(set(boundaries)) == len(boundaries)
    assert all(0 < b < 1000 for b in boundaries)


# -- ffmpeg-backed reading -------------------------------------------------


def test_duration_is_probed(tmp_path: Path):
    wav = make_audio(tmp_path / "a.wav", [(3.0, True)])
    assert probe_duration(wav) == pytest.approx(3.0, abs=0.1)


def test_probe_rejects_a_file_that_is_not_audio(tmp_path: Path):
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"definitely not audio")
    with pytest.raises(ChunkingError):
        probe_duration(junk)


def test_rms_profile_tracks_loud_and_quiet_regions(tmp_path: Path):
    wav = make_audio(tmp_path / "a.wav", [(1.0, True), (1.0, False), (1.0, True)])

    rms = rms_profile(wav)

    assert len(rms) == pytest.approx(30, abs=2)  # 3 s of 100 ms windows
    assert rms[:8].mean() > 100
    assert rms[12:18].mean() < 1


def test_rms_profile_reads_opus_too(tmp_path: Path):
    """The recorder ships Opus, so the splitter must handle it."""
    opus = make_audio(tmp_path / "a.opus", [(1.0, True), (1.0, False)])
    rms = rms_profile(opus)
    assert rms.size > 0
    assert rms[:8].mean() > 100


def test_extract_cuts_the_requested_span(tmp_path: Path):
    src = make_audio(tmp_path / "a.wav", [(6.0, True)])
    out = extract_chunk(src, tmp_path / "part.wav", offset=2.0, duration=3.0)
    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)


def test_chunks_are_written_as_16k_mono(tmp_path: Path):
    """Whisper resamples to this anyway, so cutting to it costs nothing."""
    import wave

    src = make_audio(tmp_path / "a.wav", [(4.0, True)])
    out = extract_chunk(src, tmp_path / "part.wav", offset=0.0, duration=2.0)
    with wave.open(str(out), "rb") as reader:
        assert reader.getframerate() == ANALYSIS_RATE
        assert reader.getnchannels() == 1


# -- splitting -------------------------------------------------------------


def test_short_track_is_passed_through_without_copying(tmp_path: Path):
    src = make_audio(tmp_path / "short.opus", [(2.0, True)])

    chunks = split_audio(src, tmp_path / "_chunks", target_seconds=60)

    assert len(chunks) == 1
    assert chunks[0].path == src  # the original, not a copy
    assert chunks[0].offset == 0.0
    assert not (tmp_path / "_chunks").exists()


def test_long_track_is_split_with_contiguous_offsets(tmp_path: Path):
    src = make_audio(tmp_path / "long.opus", [(2.0, True), (1.0, False)] * 6)  # 18 s

    chunks = split_audio(src, tmp_path / "_chunks", target_seconds=6, search_seconds=1.5)

    assert len(chunks) >= 3
    assert chunks[0].offset == 0.0
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.offset == pytest.approx(previous.offset + previous.duration, abs=0.01)
    assert sum(c.duration for c in chunks) == pytest.approx(18.0, abs=0.3)


def test_split_preserves_the_full_duration(tmp_path: Path):
    src = make_audio(tmp_path / "long.wav", [(2.0, True), (1.0, False)] * 4)  # 12 s

    chunks = split_audio(src, tmp_path / "_chunks", target_seconds=5, search_seconds=1.0)
    total = sum(probe_duration(c.path) for c in chunks)

    assert total == pytest.approx(12.0, abs=0.3)


def test_boundaries_prefer_silence(tmp_path: Path):
    """A cut should land in a gap, not mid-word."""
    src = make_audio(tmp_path / "long.wav", [(2.0, True), (1.0, False)] * 6)

    chunks = split_audio(src, tmp_path / "_chunks", target_seconds=6, search_seconds=1.5)

    # Silences run 2-3 s, 5-6 s, 8-9 s ... every cut should sit inside one.
    for chunk in chunks[1:]:
        position = chunk.offset % 3.0
        assert 1.8 <= position <= 3.05 or position <= 0.15, f"cut at {chunk.offset}s is mid-tone"


def test_cleanup_deletes_chunks_but_never_the_source(tmp_path: Path):
    src = make_audio(tmp_path / "long.wav", [(2.0, True), (1.0, False)] * 4)
    chunks = split_audio(src, tmp_path / "_chunks", target_seconds=5)

    cleanup_chunks(chunks, src)

    assert src.exists()
    assert not any(c.path.exists() for c in chunks)


def test_chunking_disabled_by_zero(tmp_path: Path):
    src = make_audio(tmp_path / "a.wav", [(4.0, True)])
    chunks = split_audio(src, tmp_path / "_chunks", target_seconds=0)
    assert len(chunks) == 1 and chunks[0].path == src


# -- integration with the job runner --------------------------------------


class OneSegmentPerChunk:
    """Returns a segment at a fixed position inside whatever file it is given."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe_file(self, path: Path, language: str, initial_prompt=None):
        self.calls.append(path)
        return [RawSegment(1.0, 2.0, f"chunk {len(self.calls)}")]


def test_chunk_timestamps_are_rebased_onto_the_full_track(tmp_path: Path):
    src = make_audio(tmp_path / "10.opus", [(2.0, True), (1.0, False)] * 6)
    fake = OneSegmentPerChunk()

    segments = transcribe_one(src, fake, "tr", None, chunk_seconds=6)

    assert len(fake.calls) >= 3
    assert segments[0].start == 1.0
    assert segments[1].start >= 6.0
    assert segments[-1].start > 12.0
    assert [s.start for s in segments] == sorted(s.start for s in segments)
    assert not any(p.exists() for p in fake.calls)
    assert src.exists()


def test_short_track_is_transcribed_in_one_pass(tmp_path: Path):
    src = make_audio(tmp_path / "10.opus", [(2.0, True)])
    fake = OneSegmentPerChunk()

    segments = transcribe_one(src, fake, "tr", None, chunk_seconds=600)

    assert fake.calls == [src]
    assert segments[0].start == 1.0


def test_unreadable_file_falls_back_to_whole_file(tmp_path: Path):
    junk = tmp_path / "10.opus"
    junk.write_bytes(b"not audio at all")
    fake = OneSegmentPerChunk()

    segments = transcribe_one(junk, fake, "tr", None, chunk_seconds=1)

    assert fake.calls == [junk]
    assert len(segments) == 1


def test_chunk_dataclass_is_comparable(tmp_path: Path):
    assert Chunk(tmp_path / "a", 0.0, 1.0) == Chunk(tmp_path / "a", 0.0, 1.0)
