"""Splitting a long speaker track into bounded-memory chunks.

faster-whisper decodes the whole input file into memory before it starts
streaming segments out, at roughly 33 MB per minute of 48 kHz stereo audio. A
four-hour session would therefore need ~8 GB for a single speaker - far past
what a small self-hosted box has. Splitting each track into fixed-length chunks
caps peak memory at the chunk size regardless of how long the session ran.

Everything goes through ffmpeg rather than Python's `wave` module, so the input
can be Opus, WAV or anything else ffmpeg reads. Chunks are written as 16 kHz
mono WAV, which is exactly what Whisper resamples to anyway - so this costs no
accuracy and cuts decode memory further.

Boundaries are nudged onto the quietest point near the target time so a chunk
edge does not land in the middle of a word.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RMS_WINDOW_SECONDS = 0.1
CHUNK_DIR_NAME = "_chunks"

# What we hand to Whisper. It resamples to exactly this internally.
ANALYSIS_RATE = 16_000


class ChunkingError(RuntimeError):
    """Raised when audio cannot be inspected or split."""


@dataclass(frozen=True)
class Chunk:
    """One piece of a speaker track, and where it sits in the original file."""

    path: Path
    offset: float
    duration: float


def probe_duration(path: Path) -> float:
    """Length in seconds, via ffprobe."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ChunkingError(f"ffprobe failed on {path.name}: {result.stderr.strip()}")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ChunkingError(f"Could not read a duration from {path.name}") from exc


def rms_profile(path: Path, window_seconds: float = RMS_WINDOW_SECONDS) -> np.ndarray:
    """Loudness per short window, streamed through ffmpeg so memory stays flat."""
    window_frames = max(1, int(ANALYSIS_RATE * window_seconds))
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-ac",
        "1",
        "-ar",
        str(ANALYSIS_RATE),
        "-f",
        "s16le",
        "-",
    ]
    values: list[float] = []
    leftover = b""
    block_size = window_frames * 2 * 200  # bytes; ~20 s of audio at a time

    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        assert process.stdout is not None
        while True:
            block = process.stdout.read(block_size)
            if not block:
                break
            data = leftover + block
            usable = len(data) - (len(data) % 2)
            samples = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32)
            leftover = data[usable:]
            for start in range(0, len(samples) - window_frames + 1, window_frames):
                window = samples[start : start + window_frames]
                values.append(float(np.sqrt(np.mean(window * window))))
            remainder = len(samples) % window_frames
            if remainder:
                leftover = samples[-remainder:].astype("<i2").tobytes() + leftover
        stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
        if process.wait() != 0:
            raise ChunkingError(f"ffmpeg failed reading {path.name}: {stderr.strip()}")

    return np.asarray(values, dtype=np.float32)


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


def extract_chunk(src: Path, target: Path, offset: float, duration: float) -> Path:
    """Cut one span out as 16 kHz mono WAV."""
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        # Seek before -i so long files do not decode from the start every time.
        "-ss",
        f"{offset:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        str(ANALYSIS_RATE),
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ChunkingError(f"ffmpeg failed extracting from {src.name}: {result.stderr.strip()}")
    return target


def split_audio(
    src: Path,
    out_dir: Path,
    target_seconds: float,
    search_seconds: float = 20.0,
    window_seconds: float = RMS_WINDOW_SECONDS,
) -> list[Chunk]:
    """Split a track into sequential chunks. Returns them in order.

    A track already shorter than the target is returned as a single chunk
    pointing at the original file, with nothing copied.
    """
    src = Path(src)
    duration = probe_duration(src)

    if target_seconds <= 0 or duration <= target_seconds:
        return [Chunk(path=src, offset=0.0, duration=duration)]

    rms = rms_profile(src, window_seconds)
    total_windows = max(1, int(round(duration / window_seconds)))
    # Never search further than a quarter of a chunk, or boundaries drift far
    # from the target length and chunk sizes stop being bounded.
    search_seconds = min(search_seconds, target_seconds / 4)
    boundaries = plan_boundaries(
        rms,
        total_windows,
        target_windows=max(1, int(target_seconds / window_seconds)),
        search_windows=max(1, int(search_seconds / window_seconds)),
    )

    cut_points = [0.0, *[b * window_seconds for b in boundaries], duration]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[Chunk] = []

    for index in range(len(cut_points) - 1):
        start, end = cut_points[index], cut_points[index + 1]
        span = end - start
        if span <= 0:
            continue
        target = out_dir / f"{src.stem}.part{index:03d}.wav"
        extract_chunk(src, target, start, span)
        chunks.append(Chunk(path=target, offset=round(start, 3), duration=round(span, 3)))

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
