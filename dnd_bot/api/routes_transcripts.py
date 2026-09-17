"""A finished session's transcript, one page of segments at a time."""

from __future__ import annotations

import asyncio
import re

from aiohttp import web

from ..transcripts import TranscriptMissing, TranscriptReader
from .keys import BOT, TRANSCRIPTS
from .middleware import ApiError, redact_paths

routes = web.RouteTableDef()

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_PAGE = 1000
DEFAULT_PAGE = 500


def _int(request: web.Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise ApiError(400, "bad_request", f"{name} must be between {low} and {high}.")
    return int(raw)


@routes.get("/api/v1/sessions/{session_id}/transcript")
async def transcript(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    if not SESSION_ID.match(session_id):
        raise ApiError(404, "not_found", "No such session.")
    offset = _int(request, "offset", 0, 0, 1_000_000)
    limit = _int(request, "limit", DEFAULT_PAGE, 1, MAX_PAGE)

    row = await request.app[BOT].db.get_session(session_id)
    if row is None:
        raise ApiError(404, "not_found", "No such session.")

    reader: TranscriptReader = request.app[TRANSCRIPTS]
    try:
        parsed = await asyncio.to_thread(reader.read, session_id)
    except TranscriptMissing as exc:
        raise ApiError(404, "no_transcript", "This session has no transcript yet.") from exc

    total = len(parsed.segments)
    return web.json_response(
        {
            "session": {
                "id": row["id"],
                "name": row["name"],
                "started_at": row["start_time"],
                "ended_at": row["end_time"],
                **parsed.meta,
                "warnings": [redact_paths(w) for w in parsed.meta["warnings"]],
            },
            "total": total,
            "offset": offset,
            "limit": limit,
            "segments": parsed.segments[offset : offset + limit],
        }
    )
