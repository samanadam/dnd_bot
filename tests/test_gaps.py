"""The gap sidecar is how squashed silence is put back at finalize."""

from __future__ import annotations

from pathlib import Path

from dnd_bot.audio import BYTES_PER_SECOND
from dnd_bot.gaps import (
    append_gap,
    gaps_path,
    read_gaps,
    silence_bytes_for,
    total_gap_bytes,
)


def test_gaps_path_sits_beside_the_capture(tmp_path: Path):
    assert gaps_path(tmp_path / "10.pcm") == tmp_path / "10.gaps"


def test_silence_bytes_are_whole_frames():
    assert silence_bytes_for(1.0) == BYTES_PER_SECOND
    assert silence_bytes_for(0.0000123) % 4 == 0
    assert silence_bytes_for(-3.0) == 0


def test_round_trip_sorted_and_summed(tmp_path: Path):
    path = tmp_path / "10.gaps"
    with path.open("ab") as handle:
        append_gap(handle, 7680, 192_000)
        append_gap(handle, 3840, 96_000)
    assert read_gaps(path) == [(3840, 96_000), (7680, 192_000)]
    assert total_gap_bytes(path) == 288_000


def test_missing_file_means_no_gaps(tmp_path: Path):
    assert read_gaps(tmp_path / "nope.gaps") == []
    assert total_gap_bytes(tmp_path / "nope.gaps") == 0


def test_torn_and_garbage_lines_are_ignored(tmp_path: Path):
    path = tmp_path / "10.gaps"
    path.write_bytes(b"3840 96000\nnot a line\n7680 19\n9999 192000")  # last line has no newline
    # "7680 19" aligns down to 16 bytes; the torn final line (no newline) is dropped.
    assert read_gaps(path) == [(3840, 96_000), (7680, 16)]


def test_zero_length_gaps_are_dropped(tmp_path: Path):
    path = tmp_path / "10.gaps"
    path.write_bytes(b"3840 0\n3840 3\n")
    assert read_gaps(path) == []
