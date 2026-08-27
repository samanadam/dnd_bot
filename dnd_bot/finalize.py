"""Turning raw per-speaker PCM captures into finished audio files.

Shared by the normal `/session stop` path, the auto-stop path, graceful
shutdown, and `/session recover` after a crash.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import paths
from .audio import AudioError, finalize_capture, pcm_duration_seconds

log = logging.getLogger(__name__)


def raw_captures(sessions_root: Path, session_id: str) -> list[Path]:
    raw = paths.raw_dir(sessions_root, session_id)
    if not raw.is_dir():
        return []
    return sorted(p for p in raw.glob("*.pcm") if p.is_file())


def has_raw_audio(sessions_root: Path, session_id: str) -> bool:
    return any(path.stat().st_size > 0 for path in raw_captures(sessions_root, session_id))


def finalize_session_audio(
    sessions_root: Path, session_id: str, audio_format: str = "wav", *, keep_raw: bool = False
) -> tuple[list[Path], list[str]]:
    """Finalize every speaker capture. Returns (written files, warnings).

    A single unreadable capture is reported as a warning rather than aborting the
    whole session - one broken speaker file should not cost everyone else their
    transcript.
    """
    written: list[Path] = []
    warnings: list[str] = []
    for pcm_path in raw_captures(sessions_root, session_id):
        user_id = pcm_path.stem
        out_path = paths.finalized_audio_path(sessions_root, session_id, user_id, audio_format)
        try:
            finalize_capture(pcm_path, out_path, audio_format)
        except AudioError as exc:
            warnings.append(f"Speaker {user_id}: {exc}")
            log.warning("Could not finalize capture for %s: %s", user_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - keep going for the other speakers
            warnings.append(f"Speaker {user_id}: unexpected error: {exc}")
            log.exception("Unexpected failure finalizing capture for %s", user_id)
            continue
        written.append(out_path)
        if not keep_raw:
            pcm_path.unlink(missing_ok=True)
    return written, warnings


def captured_seconds(sessions_root: Path, session_id: str) -> float:
    """Longest single-speaker capture length, from raw PCM byte counts."""
    durations = [
        pcm_duration_seconds(path.stat().st_size)
        for path in raw_captures(sessions_root, session_id)
    ]
    return max(durations, default=0.0)
