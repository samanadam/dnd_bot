"""Audio finalization helpers.

Discord hands us raw PCM (48 kHz, stereo, signed 16-bit little endian). During a
session that PCM is appended straight to disk; a container header is only added
at finalize time so a crash mid-session still leaves a repairable file.
"""

from __future__ import annotations

import logging
import subprocess
import wave
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


def pcm_to_wav(pcm_path: Path, wav_path: Path) -> Path:
    """Wrap a raw PCM capture in a WAV header."""
    pcm_path = Path(pcm_path)
    wav_path = Path(wav_path)
    if not pcm_path.exists():
        raise AudioError(f"Raw capture missing: {pcm_path}")
    if pcm_path.stat().st_size == 0:
        raise AudioError(f"Raw capture is empty: {pcm_path}")

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(SAMPLE_RATE)
        with pcm_path.open("rb") as source:
            while chunk := source.read(1 << 20):
                # A truncated final frame would make the WAV unreadable.
                usable = len(chunk) - (len(chunk) % (CHANNELS * SAMPLE_WIDTH))
                writer.writeframes(chunk[:usable])
    return wav_path


def wav_to_opus(wav_path: Path, opus_path: Path, bitrate_kbps: int = 48) -> Path:
    """Transcode to Opus, ~50x smaller than the raw capture.

    Discord already delivers Opus and py-cord decodes it to PCM, so this mostly
    undoes an expansion we caused ourselves. Mono at 48 kbps is generous for
    speech and is what makes shipping a session over a home connection - and
    storing it on a small VPS - practical at all.
    """
    wav_path = Path(wav_path)
    opus_path = Path(opus_path)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(wav_path),
        "-ac",
        "1",
        "-c:a",
        "libopus",
        "-b:a",
        f"{bitrate_kbps}k",
        # Tuned for speech rather than music.
        "-application",
        "voip",
        str(opus_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AudioError(f"ffmpeg failed encoding {wav_path.name} to Opus: {result.stderr.strip()}")
    return opus_path


def wav_to_mp3(wav_path: Path, mp3_path: Path) -> Path:
    """Transcode with ffmpeg. Used only when AUDIO_FORMAT=mp3."""
    wav_path = Path(wav_path)
    mp3_path = Path(mp3_path)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(wav_path),
        "-codec:a",
        "libmp3lame",
        "-qscale:a",
        "4",
        str(mp3_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AudioError(f"ffmpeg failed for {wav_path.name}: {result.stderr.strip()}")
    return mp3_path


ENCODERS = {"mp3": wav_to_mp3, "opus": wav_to_opus}


def finalize_capture(pcm_path: Path, out_path: Path, audio_format: str = "opus") -> Path:
    """Turn one user's raw PCM into the configured deliverable format."""
    if audio_format == "wav":
        return pcm_to_wav(pcm_path, out_path)

    encoder = ENCODERS.get(audio_format)
    if encoder is None:
        raise AudioError(f"Unsupported audio format {audio_format!r}")

    intermediate = out_path.with_suffix(".intermediate.wav")
    pcm_to_wav(pcm_path, intermediate)
    try:
        encoder(intermediate, out_path)
    finally:
        intermediate.unlink(missing_ok=True)
    return out_path
