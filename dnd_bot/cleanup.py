"""Retention cleanup and disk monitoring.

Only `sessions/<id>/audio/` is ever removed. Transcripts, database rows and
anything under `exports/` are kept until a human deletes them.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths
from .chunking import CHUNK_DIR_NAME
from .timeutil import from_iso

log = logging.getLogger(__name__)


def expired_sessions(rows: Sequence[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Sessions whose audio retention window has closed."""
    expired: list[dict[str, Any]] = []
    for row in rows:
        expires_at = from_iso(row.get("audio_expires_at"))
        if expires_at is not None and expires_at <= now:
            expired.append(row)
    return expired


def directory_size_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def delete_session_audio(sessions_root: Path, session_id: str) -> int:
    """Delete raw + finalized audio for one session. Returns bytes freed."""
    audio_dir = paths.audio_dir(sessions_root, session_id)
    if not audio_dir.is_dir():
        return 0
    freed = directory_size_bytes(audio_dir)
    shutil.rmtree(audio_dir, ignore_errors=True)
    log.info("Deleted audio for session %s (%.1f MB)", session_id, freed / 1_000_000)
    return freed


def purge_orphaned_chunks(sessions_root: Path) -> int:
    """Remove chunk directories left behind by a crash mid-transcription.

    Chunks are duplicates of audio already on disk, so deleting them is always
    safe - a re-run simply splits the track again.
    """
    if not sessions_root.is_dir():
        return 0
    freed = 0
    for chunk_dir in sessions_root.glob(f"*/audio/{CHUNK_DIR_NAME}"):
        if not chunk_dir.is_dir():
            continue
        freed += directory_size_bytes(chunk_dir)
        shutil.rmtree(chunk_dir, ignore_errors=True)
        log.info("Removed orphaned chunk directory %s", chunk_dir)
    return freed


def free_space_mb(path: Path) -> float:
    usage = shutil.disk_usage(str(path))
    return usage.free / 1_000_000


async def run_cleanup(db, config, notifier=None, now: datetime | None = None) -> dict[str, Any]:
    """One retention pass plus a disk-space check."""
    from .timeutil import utcnow

    now = now or utcnow()
    rows = await db.list_sessions_with_audio()
    freed_total = purge_orphaned_chunks(config.sessions_dir)
    cleaned: list[str] = []

    for row in expired_sessions(rows, now):
        session_id = row["id"]
        freed_total += delete_session_audio(config.sessions_dir, session_id)
        await db.update_session(session_id, audio_expires_at=None)
        cleaned.append(session_id)

    free_mb = free_space_mb(config.data_dir)
    low_disk = free_mb < config.disk_warning_threshold_mb
    if low_disk and notifier is not None:
        message = (
            f"Low disk space on the bot host: {free_mb:.0f} MB free "
            f"(threshold {config.disk_warning_threshold_mb} MB). "
            "Recordings may start failing."
        )
        recent = await db.list_sessions(limit=1)
        session = recent[0] if recent else {}
        await notifier.send_channel(
            int(session["text_channel_id"]) if session.get("text_channel_id") else None, message
        )
        owner = session.get("started_by_user_id")
        await notifier.send_dm(int(owner) if owner else config.admin_user_id, message)

    log.info(
        "Cleanup pass done: %s sessions cleaned, %.1f MB freed, %.0f MB free",
        len(cleaned),
        freed_total / 1_000_000,
        free_mb,
    )
    return {
        "cleaned": cleaned,
        "freed_bytes": freed_total,
        "free_mb": free_mb,
        "low_disk": low_disk,
    }
