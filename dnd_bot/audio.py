"""Audio finalization helpers.

Discord hands us raw PCM (48 kHz, stereo, signed 16-bit little endian). During a
session that PCM is appended straight to disk; a container header is only added
at finalize time so a crash mid-session still leaves a repairable file.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import tempfile
import wave
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH


class AudioError(RuntimeError):
    """Raised when a raw capture cannot be turned into a playable file."""


def pcm_duration_seconds(pcm_bytes: int) -> float:
    return pcm_bytes / BYTES_PER_SECOND


SILENCE_BLOCK = bytes(1 << 20)


def _silence_chunks(silence_bytes: int) -> Iterator[bytes]:
    """Zeros in bounded pieces, so a long pause never allocates a long buffer."""
    remaining = silence_bytes - silence_bytes % (CHANNELS * SAMPLE_WIDTH)
    while remaining > 0:
        take = min(remaining, len(SILENCE_BLOCK))
        yield SILENCE_BLOCK[:take]
        remaining -= take


def iter_padded_pcm(pcm_path: Path, gaps: Sequence[tuple[int, int]] = ()) -> Iterator[bytes]:
    """A raw capture's frames with the pauses it left out put back as silence.

    `gaps` is `(pcm_position, silence_bytes)` sorted by position. Streaming it
    means a long pause costs time, not disk: nothing the length of the session
    is ever held in memory or written out.
    """
    frame = CHANNELS * SAMPLE_WIDTH
    pending = deque(gaps)
    position = 0
    with Path(pcm_path).open("rb") as source:
        while chunk := source.read(1 << 20):
            # A truncated final frame would make the output unreadable.
            chunk = chunk[: len(chunk) - (len(chunk) % frame)]
            start = 0
            while pending and pending[0][0] < position + len(chunk):
                gap_position, silence = pending.popleft()
                cut = max(start, gap_position - position)
                if cut > start:
                    yield chunk[start:cut]
                yield from _silence_chunks(silence)
                start = cut
            if start < len(chunk):
                yield chunk[start:]
            position += len(chunk)
    # A gap recorded past the capture's end means the tail never reached disk.
    for _, silence in pending:
        yield from _silence_chunks(silence)


def _require_capture(pcm_path: Path) -> None:
    if not pcm_path.exists():
        raise AudioError(f"Raw capture missing: {pcm_path}")
    if pcm_path.stat().st_size == 0:
        raise AudioError(f"Raw capture is empty: {pcm_path}")


def pcm_to_wav(pcm_path: Path, wav_path: Path, gaps: Sequence[tuple[int, int]] = ()) -> Path:
    """Wrap a raw PCM capture in a WAV header, with its pauses put back."""
    pcm_path = Path(pcm_path)
    wav_path = Path(wav_path)
    _require_capture(pcm_path)

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(SAMPLE_RATE)
        for piece in iter_padded_pcm(pcm_path, gaps):
            writer.writeframes(piece)
    return wav_path


# Mono at 48 kbps is generous for speech and is what makes shipping a session
# over a home connection - and storing it on a small VPS - practical at all.
# Not "-application voip": it spends the full bitrate even on digital silence,
# and a track now carries every pause. That made an hour of silence ~10 MB and
# ~3x slower to encode than "audio", which codes it in under a tenth of that.
# Speech costs the same either way at this bitrate.
OPUS_ARGS = ["-ac", "1", "-c:a", "libopus", "-b:a", "48k", "-application", "audio"]
MP3_ARGS = ["-codec:a", "libmp3lame", "-qscale:a", "4"]
ENCODER_ARGS = {"mp3": MP3_ARGS, "opus": OPUS_ARGS}


def encode_stream(
    pieces: Iterable[bytes], out_path: Path, codec_args: Sequence[str], label: str
) -> Path:
    """Pipe raw frames into ffmpeg and encode them straight to `out_path`.

    Discord already delivers Opus and py-cord decodes it to PCM, so encoding
    mostly undoes an expansion we caused ourselves. Feeding ffmpeg through a
    pipe, rather than staging a WAV first, is what keeps a long session's
    silence off the disk.
    """
    out_path = Path(out_path)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-i",
        "pipe:0",
        *codec_args,
        str(out_path),
    ]
    # stderr goes to a file, not a pipe: a pipe nobody is reading blocks ffmpeg
    # once it fills, which would hang the writes below.
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=stderr)
        assert process.stdin is not None
        try:
            for piece in pieces:
                process.stdin.write(piece)
        except BrokenPipeError:
            pass  # ffmpeg quit early; its exit status and stderr say why
        finally:
            with contextlib.suppress(BrokenPipeError, OSError):
                process.stdin.close()
            returncode = process.wait()
        if returncode != 0:
            stderr.seek(0)
            detail = stderr.read().decode("utf-8", errors="replace").strip()
            out_path.unlink(missing_ok=True)
            raise AudioError(f"ffmpeg failed encoding {label}: {detail}")
    return out_path


def finalize_capture(pcm_path: Path, out_path: Path, audio_format: str = "opus") -> Path:
    """Turn one user's raw PCM into the configured deliverable format."""
    # Imported here: gaps reads this module's constants.
    from .gaps import gaps_path, read_gaps

    pcm_path = Path(pcm_path)
    gaps = read_gaps(gaps_path(pcm_path))
    if audio_format == "wav":
        return pcm_to_wav(pcm_path, out_path, gaps)

    codec_args = ENCODER_ARGS.get(audio_format)
    if codec_args is None:
        raise AudioError(f"Unsupported audio format {audio_format!r}")
    _require_capture(pcm_path)
    return encode_stream(iter_padded_pcm(pcm_path, gaps), out_path, codec_args, pcm_path.name)
