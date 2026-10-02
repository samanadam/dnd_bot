"""Sidecar record of the silence between a speaker's bursts of speech.

Discord stops sending packets while someone is quiet, and the decoder hands
over only the speech, so a raw capture is every burst glued end to end. The
sink writes each pause here as it notices it - "at this byte of the capture,
this much silence belongs" - and finalize puts the silence back. The capture
itself stays speech-only: a four-hour game with long pauses costs far less
disk than padding as it goes, and a crash cannot corrupt it.

One line per gap, `<pcm_position> <silence_bytes>`, both whole frames.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from .audio import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH

FRAME_BYTES = CHANNELS * SAMPLE_WIDTH


def gaps_path(pcm_path: Path) -> Path:
    return Path(pcm_path).with_suffix(".gaps")


def silence_bytes_for(seconds: float) -> int:
    """Whole frames of silence covering `seconds` (never negative)."""
    return max(0, round(seconds * SAMPLE_RATE)) * FRAME_BYTES


def append_gap(handle: BinaryIO, pcm_position: int, silence_bytes: int) -> None:
    handle.write(f"{pcm_position} {silence_bytes}\n".encode("ascii"))


def read_gaps(path: Path) -> list[tuple[int, int]]:
    """Every usable gap, sorted by position.

    A crash can leave a torn last line, and a line is only trusted once its
    newline is there. Anything unparsable is skipped: a lost gap costs a little
    timing, an exception here would cost the session's audio.
    """
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_bytes().decode("ascii", errors="ignore")
    lines = text.split("\n")[:-1]  # the piece after the last newline is untrusted
    gaps: list[tuple[int, int]] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            position, silence = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        position -= position % FRAME_BYTES
        silence -= silence % FRAME_BYTES
        if position < 0 or silence <= 0:
            continue
        gaps.append((position, silence))
    return sorted(gaps)


def total_gap_bytes(path: Path) -> int:
    return sum(silence for _, silence in read_gaps(path))
