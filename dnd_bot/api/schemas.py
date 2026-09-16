"""The only place internal objects become API responses.

Every function here builds its result field by field. That is deliberate: a
`sessions` row carries `started_by_user_id`, `participants_json` and
`offsets_json`, each holding real Discord user ids, and the API answers
requests from the public internet. Returning `dict(row)` anywhere would put
those ids on the wire the first time somebody added a column.

So: speakers are reported as display labels and a count, never as ids, and no
response carries a filesystem path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .. import __version__
from ..timeutil import from_iso


def _labels(raw: str | None) -> list[str]:
    """Speaker display names out of a participants_json blob, ids dropped."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    return sorted(str(label) for label in parsed.values())


def _duration(start: str | None, end: str | None) -> float | None:
    """Session length. Not a stored column - both ends are timestamps."""
    started, ended = from_iso(start), from_iso(end)
    if started is None or ended is None:
        return None
    return (ended - started).total_seconds()


def session_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """One finished session, as the portal's session list shows it."""
    speakers = _labels(row.get("participants_json"))
    return {
        "id": row["id"],
        "name": row["name"],
        "channel_name": row["channel_name"],
        "started_at": row["start_time"],
        "ended_at": row["end_time"],
        "duration_seconds": _duration(row["start_time"], row["end_time"]),
        "transcribed": bool(row["transcribed"]),
        "cancelled": bool(row["cancelled"]),
        "speakers": speakers,
        "speaker_count": len(speakers),
    }


def active_session(session: Any) -> dict[str, Any]:
    """One live recording.

    `channel_id` is here because the portal addresses /recording/stop by
    channel; it is the one id that has to travel.
    """
    labels = sorted(str(label) for label in getattr(session, "labels", {}).values())
    return {
        "session_id": session.session_id,
        "name": session.name,
        "channel_id": str(session.channel_id),
        "channel_name": session.channel_name,
        "started_at": session.start_time.isoformat() if session.start_time else None,
        "elapsed_seconds": session.elapsed_seconds(),
        "speakers": labels,
        "speaker_count": len(labels),
        "warnings": list(getattr(session, "warnings", [])),
    }


def health(*, ready: bool, uptime_seconds: float, db_ok: bool) -> dict[str, Any]:
    """Liveness. Answers even when the database is unreachable.

    No version here: this is the one route served without a token, and a
    version number tells a stranger which advisories to go and read. It is on
    /stats instead, where a caller has already proved they are allowed to know.
    """
    return {
        "status": "ok" if db_ok else "degraded",
        "ready": ready,
        "uptime_seconds": int(uptime_seconds),
        "db": "ok" if db_ok else "unreachable",
    }


def stats(
    *,
    active: list[dict[str, Any]],
    pending_transcription: int,
    free_mb: float,
    data_bytes: int,
    low_disk: bool,
    storage_backend: str,
    storage_reachable: bool | None,
    music: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "version": __version__,
        "sessions": {
            "active": active,
            "pending_transcription": pending_transcription,
        },
        "disk": {
            "free_mb": free_mb,
            "data_bytes": data_bytes,
            "low_disk": low_disk,
        },
        "storage": {
            "backend": storage_backend,
            "reachable": storage_reachable,
        },
        "music": music,
    }


def error(code: str, message: str) -> dict[str, Any]:
    """The single error shape. Messages are for operators, not stack traces."""
    return {"error": {"code": code, "message": message}}
