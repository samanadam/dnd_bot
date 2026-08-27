"""Filesystem layout for session data. Pure path math, no I/O side effects."""

from __future__ import annotations

from pathlib import Path


def session_dir(sessions_root: Path, session_id: str) -> Path:
    return sessions_root / session_id


def audio_dir(sessions_root: Path, session_id: str) -> Path:
    return session_dir(sessions_root, session_id) / "audio"


def raw_dir(sessions_root: Path, session_id: str) -> Path:
    return audio_dir(sessions_root, session_id) / "raw"


def raw_pcm_path(sessions_root: Path, session_id: str, user_id: str) -> Path:
    return raw_dir(sessions_root, session_id) / f"{user_id}.pcm"


def finalized_audio_path(
    sessions_root: Path, session_id: str, user_id: str, audio_format: str = "wav"
) -> Path:
    return audio_dir(sessions_root, session_id) / f"{user_id}.{audio_format}"


def transcript_md_path(sessions_root: Path, session_id: str) -> Path:
    return session_dir(sessions_root, session_id) / "transcript.md"


def transcript_json_path(sessions_root: Path, session_id: str) -> Path:
    return session_dir(sessions_root, session_id) / "transcript.json"


def export_path(exports_root: Path, session_id: str) -> Path:
    return exports_root / f"{session_id}.zip"


def ensure_session_dirs(sessions_root: Path, session_id: str) -> None:
    raw_dir(sessions_root, session_id).mkdir(parents=True, exist_ok=True)
