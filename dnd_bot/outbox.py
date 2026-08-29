"""Publishing a finished session for the transcriber to collect.

The recorder never transcribes. It captures audio, describes it, and drops the
result into the outbox. Whether anyone ever collects it is not its problem -
which is what lets the transcriber be a laptop in another country that is
switched on twice a week.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from . import paths
from .contract import (
    READY_MARKER,
    SessionMetadata,
    is_marked,
    mark,
    ready_sessions,
    write_metadata,
)

log = logging.getLogger(__name__)


def outbox_dir(outbox_root: Path, session_id: str) -> Path:
    return Path(outbox_root) / session_id


def metadata_from_session(
    session: dict[str, Any],
    *,
    timezone_name: str,
    prompt_extra: str = "",
    audio_format: str = "opus",
) -> SessionMetadata:
    """Freeze everything the transcriber will need into a portable record."""
    return SessionMetadata(
        session_id=session["id"],
        name=session.get("name") or f"Session {str(session['id'])[:8]}",
        start_time_utc=session["start_time"],
        end_time_utc=session.get("end_time"),
        timezone=timezone_name,
        channel_name=session.get("channel_name"),
        participants=json.loads(session.get("participants_json") or "{}"),
        offsets={
            key: float(value)
            for key, value in json.loads(session.get("offsets_json") or "{}").items()
        },
        language=session.get("language") or "tr",
        prompt_extra=prompt_extra,
        audio_format=audio_format,
    )


def publish(
    session: dict[str, Any],
    *,
    sessions_root: Path,
    outbox_root: Path,
    audio_format: str,
    timezone_name: str,
    prompt_extra: str = "",
    move: bool = True,
) -> Path:
    """Stage a finished session in the outbox and mark it READY.

    `move` hands the audio over rather than duplicating it, which matters when
    a session is several GB. The session directory keeps its transcripts and
    metadata; only the tracks relocate.
    """
    session_id = session["id"]
    target = outbox_dir(outbox_root, session_id)
    if is_marked(target, READY_MARKER):
        log.info("Session %s is already staged in the outbox", session_id)
        return target

    audio_dir = paths.audio_dir(sessions_root, session_id)
    tracks = sorted(audio_dir.glob(f"*.{audio_format}")) if audio_dir.is_dir() else []
    if not tracks:
        raise FileNotFoundError(
            f"No .{audio_format} tracks for session {session_id}; nothing to publish"
        )

    target.mkdir(parents=True, exist_ok=True)
    for track in tracks:
        destination = target / track.name
        if move:
            shutil.move(str(track), str(destination))
        else:
            shutil.copy2(track, destination)

    write_metadata(
        target,
        metadata_from_session(
            session,
            timezone_name=timezone_name,
            prompt_extra=prompt_extra,
            audio_format=audio_format,
        ),
    )
    # Last write: until this exists, a collector ignores the directory, so a
    # crash midway through the copy above leaves nothing half-usable.
    mark(target, READY_MARKER)

    total_mb = sum(p.stat().st_size for p in target.glob(f"*.{audio_format}")) / 1_000_000
    log.info(
        "Published session %s to the outbox (%s track(s), %.1f MB)",
        session_id,
        len(tracks),
        total_mb,
    )
    return target


def pending(outbox_root: Path) -> list[str]:
    """Session ids staged and waiting for the transcriber to collect them."""
    return [directory.name for directory in ready_sessions(outbox_root)]


def discard(outbox_root: Path, session_id: str) -> int:
    """Remove a staged session once its transcript has come back. Returns bytes freed."""
    target = outbox_dir(outbox_root, session_id)
    if not target.is_dir():
        return 0
    freed = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    shutil.rmtree(target, ignore_errors=True)
    log.info(
        "Removed collected session %s from the outbox (%.1f MB)", session_id, freed / 1_000_000
    )
    return freed
