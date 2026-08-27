"""Chunking is what keeps peak memory flat on a long session."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import numpy as np

from dnd_bot.chunking import (
    Chunk,
    choose_boundary,
    cleanup_chunks,
    plan_boundaries,
    rms_profile,
    split_wav,
)
from dnd_bot.jobs import transcribe_one
from dnd_bot.transcription import RawSegment

RATE = 48000


def write_wav(path: Path, pattern: list[tuple[float, bool]]) -> None:
    """pattern: [(seconds, is_loud), ...] as 48 kHz stereo 16-bit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        for seconds, loud in pattern:
            frames = int(RATE * seconds)
            if loud:
                block = b"".join(
                    struct.pack("<hh", v, v)
                    for v in (
                        int(9000 * math.sin(2 * math.pi * 220 * n / RATE)) for n in range(RATE)
                    )
                )
                writer.writeframes(block * int(seconds) + block[: (frames % RATE) * 4])
            else:
                writer.writeframes(bytes(frames * 4))


def read_frames(path: Path) -> bytes:
    with wave.open(str(path), "rb") as reader:
        return reader.readframes(reader.getnframes())


def test_rms_profile_tracks_loud_and_quiet_regions(tmp_path: Path):
    wav = tmp_path / "a.wav"
    write_wav(wav, [(1.0, True), (1.0, False), (1.0, True)])

    rms, rate = rms_profile(wav)

    assert rate == RATE
    assert len(rms) == 30  # 3 seconds of 100 ms windows
    assert rms[:10].mean() > 100
    assert rms[10:20].mean() == 0.0


def test_boundary_snaps_to_the_quietest_nearby_window():
    rms = np.array([9.0, 9.0, 9.0, 0.5, 9.0, 9.0, 9.0], dtype=np.float32)
    # Target index 5, silence at 3 - within the search window, so it moves there.
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


def test_short_file_is_passed_through_without_copying(tmp_path: Path):
    wav = tmp_path / "short.wav"
    write_wav(wav, [(2.0, True)])

    chunks = split_wav(wav, tmp_path / "_chunks", target_seconds=60)

    assert len(chunks) == 1
    assert chunks[0].path == wav  # the original, not a copy
    assert chunks[0].offset == 0.0
    assert not (tmp_path / "_chunks").exists()


def test_long_file_is_split_with_correct_offsets(tmp_path: Path):
    wav = tmp_path / "long.wav"
    # Loud / quiet alternating so there are real silences to cut on.
    write_wav(wav, [(2.0, True), (1.0, False)] * 6)  # 18 s total

    chunks = split_wav(wav, tmp_path / "_chunks", target_seconds=6, search_seconds=1.5)

    assert len(chunks) >= 3
    assert chunks[0].offset == 0.0
    # Offsets are contiguous: each chunk starts where the previous one ended.
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.offset == round(previous.offset + previous.duration, 6)
    assert sum(c.duration for c in chunks) == 18.0


def test_split_preserves_every_sample(tmp_path: Path):
    wav = tmp_path / "long.wav"
    write_wav(wav, [(2.0, True), (1.0, False)] * 4)

    chunks = split_wav(wav, tmp_path / "_chunks", target_seconds=5, search_seconds=1.0)
    rejoined = b"".join(read_frames(c.path) for c in chunks)

    assert rejoined == read_frames(wav)


def test_cleanup_deletes_chunks_but_never_the_source(tmp_path: Path):
    wav = tmp_path / "long.wav"
    write_wav(wav, [(2.0, True), (1.0, False)] * 4)
    chunks = split_wav(wav, tmp_path / "_chunks", target_seconds=5)

    cleanup_chunks(chunks, wav)

    assert wav.exists()
    assert not any(c.path.exists() for c in chunks)


class OneSegmentPerChunk:
    """Returns a segment at a fixed position inside whatever file it is given."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe_file(self, path: Path, language: str, initial_prompt=None):
        self.calls.append(path)
        return [RawSegment(1.0, 2.0, f"chunk {len(self.calls)}")]


def test_chunk_timestamps_are_rebased_onto_the_full_track(tmp_path: Path):
    wav = tmp_path / "10.wav"
    write_wav(wav, [(2.0, True), (1.0, False)] * 6)
    fake = OneSegmentPerChunk()

    segments = transcribe_one(wav, fake, "tr", None, chunk_seconds=6)

    assert len(fake.calls) >= 3
    # Every segment after the first is shifted by its chunk's offset.
    assert segments[0].start == 1.0
    # Each later segment sits inside its own chunk's slot on the full timeline.
    assert segments[1].start >= 6.0
    assert segments[-1].start > 12.0
    assert [s.start for s in segments] == sorted(s.start for s in segments)
    # Temp chunk files are gone once the track is done.
    assert not any(p.exists() for p in fake.calls)
    assert wav.exists()


def test_short_track_is_transcribed_in_one_pass(tmp_path: Path):
    wav = tmp_path / "10.wav"
    write_wav(wav, [(2.0, True)])
    fake = OneSegmentPerChunk()

    segments = transcribe_one(wav, fake, "tr", None, chunk_seconds=600)

    assert fake.calls == [wav]
    assert segments[0].start == 1.0


def test_unsplittable_file_falls_back_to_whole_file(tmp_path: Path):
    """AUDIO_FORMAT=mp3 produces files `wave` cannot parse - must still work."""
    fake_mp3 = tmp_path / "10.mp3"
    fake_mp3.write_bytes(b"ID3 not really a wav")
    fake = OneSegmentPerChunk()

    segments = transcribe_one(fake_mp3, fake, "tr", None, chunk_seconds=1)

    assert fake.calls == [fake_mp3]
    assert len(segments) == 1


def test_chunking_disabled_by_zero(tmp_path: Path):
    wav = tmp_path / "10.wav"
    write_wav(wav, [(2.0, True), (1.0, False)] * 4)
    chunks = split_wav(wav, tmp_path / "_chunks", target_seconds=0)
    assert chunks == [Chunk(path=wav, offset=0.0, duration=12.0)]
