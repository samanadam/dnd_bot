"""Splitting a long speaker track into bounded-memory chunks.

faster-whisper decodes the whole input file into memory before it starts
streaming segments out, at roughly 33 MB per minute of 48 kHz stereo audio. A
four-hour session would therefore need ~8 GB for a single speaker - far past
what this box has. Splitting each track into fixed-length chunks caps peak
memory at the chunk size regardless of how long the session ran.

Boundaries are nudged onto the quietest point near the target time so a chunk
edge does not land in the middle of a word.
"""

from __future__ import annotations

import logging
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RMS_WINDOW_SECONDS = 0.1
CHUNK_DIR_NAME = "_chunks"


@dataclass(frozen=True)
class Chunk:
    """One piece of a speaker track, and where it sits in the original file."""

    path: Path
    offset: float
    duration: float


def rms_profile(path: Path, window_seconds: float = RMS_WINDOW_SECONDS) -> tuple[np.ndarray, int]:
    """Loudness per short window, computed streaming so memory stays flat."""
    with wave.open(str(path), "rb") as reader:
        rate = reader.getframerate()
        channels = reader.getnchannels()
        window_frames = max(1, int(rate * window_seconds))
        values: list[float] = []
        while True:
            raw = reader.readframes(window_frames * 50)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            if channels > 1:
                usable = len(samples) - (len(samples) % channels)
                samples = samples[:usable].reshape(-1, channels).mean(axis=1)
            for start in range(0, len(samples), window_frames):
                window = samples[start : start + window_frames]
                if len(window):
                    values.append(float(np.sqrt(np.mean(window * window))))
    return np.asarray(values, dtype=np.float32), rate


def choose_boundary(rms: np.ndarray, target_index: int, search_windows: int) -> int:
    """Quietest window within +/- search_windows of the target index."""
    if rms.size == 0:
        return target_index
    low = max(0, target_index - search_windows)
    high = min(rms.size, target_index + search_windows + 1)
    if low >= high:
        return min(target_index, rms.size)
    return int(low + np.argmin(rms[low:high]))


def plan_boundaries(
    rms: np.ndarray,
    total_windows: int,
    target_windows: int,
    search_windows: int,
) -> list[int]:
    """Window indexes at which to cut, excluding 0 and the end of the file."""
    if target_windows <= 0 or total_windows <= target_windows:
        return []
    boundaries: list[int] = []
    cursor = target_windows
    while cursor < total_windows:
        boundary = choose_boundary(rms, cursor, search_windows)
        # Never cut backwards or produce an empty chunk.
        if boundaries and boundary <= boundaries[-1]:
            boundary = cursor
        if boundary <= 0 or boundary >= total_windows:
            break
        boundaries.append(boundary)
        cursor = boundary + target_windows
    return boundaries


def split_wav(
    src: Path,
    out_dir: Path,
    target_seconds: float,
    search_seconds: float = 20.0,
    window_seconds: float = RMS_WINDOW_SECONDS,
) -> list[Chunk]:
    """Write `src` out as sequential chunks. Returns them in order.

    A file already shorter than the target is returned as a single chunk
    pointing at the original file, with nothing copied.
    """
    src = Path(src)
    with wave.open(str(src), "rb") as reader:
        rate = reader.getframerate()
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        total_frames = reader.getnframes()
    duration = total_frames / rate if rate else 0.0

    if target_seconds <= 0 or duration <= target_seconds:
        return [Chunk(path=src, offset=0.0, duration=duration)]

    rms, _ = rms_profile(src, window_seconds)
    window_frames = max(1, int(rate * window_seconds))
    total_windows = max(1, -(-total_frames // window_frames))
    # Never search further than a quarter of a chunk, or boundaries drift far
    # from the target length and chunk sizes stop being bounded.
    search_seconds = min(search_seconds, target_seconds / 4)
    boundaries = plan_boundaries(
        rms,
        total_windows,
        target_windows=max(1, int(target_seconds / window_seconds)),
        search_windows=max(1, int(search_seconds / window_seconds)),
    )

    cut_frames = [0, *[b * window_frames for b in boundaries], total_frames]
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[Chunk] = []

    with wave.open(str(src), "rb") as reader:
        for index in range(len(cut_frames) - 1):
            start, end = cut_frames[index], cut_frames[index + 1]
            frames_wanted = end - start
            if frames_wanted <= 0:
                continue
            target = out_dir / f"{src.stem}.part{index:03d}.wav"
            with wave.open(str(target), "wb") as writer:
                writer.setnchannels(channels)
                writer.setsampwidth(width)
                writer.setframerate(rate)
                remaining = frames_wanted
                while remaining > 0:
                    block = reader.readframes(min(remaining, rate * 30))
                    if not block:
                        break
                    writer.writeframes(block)
                    remaining -= len(block) // (channels * width)
            chunks.append(Chunk(path=target, offset=start / rate, duration=frames_wanted / rate))

    log.info(
        "Split %s (%.1f min) into %s chunk(s) of ~%.0f min",
        src.name,
        duration / 60,
        len(chunks),
        target_seconds / 60,
    )
    return chunks


def cleanup_chunks(chunks: list[Chunk], source: Path) -> None:
    """Delete temporary chunk files, never the original track."""
    for chunk in chunks:
        if chunk.path != source:
            chunk.path.unlink(missing_ok=True)
