"""Search across transcripts, and the transcription queue.

The queue is honest about what the bot can see. It knows a session was staged
and whether the audio is still on this host or already in the bucket; it does
not know what the transcriber is doing, because the transcriber never reports
back until a transcript is done. So the states are where the audio is, not
whether a job is healthy, and a session waiting too long is flagged rather than
declared failed.
"""

from __future__ import annotations

import asyncio
import re

from aiohttp import web

from .. import outbox
from ..search import SearchUnavailable
from ..timeutil import from_iso, utcnow
from .keys import BOT, CONFIG, SEARCH, SYNC_LOCK
from .middleware import ApiError

routes = web.RouteTableDef()

CAMPAIGN_ID = re.compile(r"^[a-f0-9]{12}$")
# The transcriber runs on a laptop that may be off for a day or two.
STALLED_AFTER_SECONDS = 48 * 3600
MAX_QUERY_CHARS = 200


def _campaign_filter(request: web.Request) -> str | None:
    campaign = request.query.get("campaign")
    if campaign is not None and campaign != "unassigned" and not CAMPAIGN_ID.match(campaign):
        raise ApiError(400, "bad_request", "campaign must be a campaign id or 'unassigned'.")
    return campaign


@routes.get("/api/v1/transcripts/search")
async def search(request: web.Request) -> web.Response:
    query = request.query.get("q", "").strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        raise ApiError(400, "bad_request", f"q must be 1-{MAX_QUERY_CHARS} characters.")
    raw_limit = request.query.get("limit", "30")
    if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 100:
        raise ApiError(400, "bad_request", "limit must be between 1 and 100.")
    campaign = _campaign_filter(request)
    try:
        hits, pending = await request.app[SEARCH].search(
            query, campaign=campaign, limit=int(raw_limit)
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
    except SearchUnavailable as exc:
        raise ApiError(
            503, "search_unavailable", "This bot's database has no full-text search."
        ) from exc
    return web.json_response(
        {"query": query, "results": [hit.to_dict() for hit in hits], "still_indexing": pending}
    )


async def _queue(request: web.Request) -> dict:
    bot = request.app[BOT]
    staged = set(outbox.pending(request.app[CONFIG].outbox_dir))
    uploads = getattr(bot, "uploader", None) is not None
    now = utcnow()
    items = []
    for row in await bot.db.awaiting_transcription():
        session = await bot.db.get_session(row["session_id"]) or {}
        queued = from_iso(row["queued_at"])
        waiting = max(0.0, (now - queued).total_seconds()) if queued else None
        if row["status"] == "transcribing":
            state = "transcribing"
        elif row["session_id"] in staged and uploads:
            state = "uploading"
        else:
            state = "waiting"
        items.append(
            {
                "session_id": row["session_id"],
                "name": row.get("name"),
                "campaign_id": session.get("campaign_id"),
                "campaign_name": session.get("campaign_name"),
                "status": state,
                "queued_at": row["queued_at"],
                "waiting_seconds": waiting,
                "stalled": waiting is not None and waiting > STALLED_AFTER_SECONDS,
            }
        )
    return {"items": items, "can_sync": uploads or getattr(bot, "fetcher", None) is not None}


@routes.get("/api/v1/transcription")
async def queue(request: web.Request) -> web.Response:
    return web.json_response(await _queue(request))


@routes.post("/api/v1/transcription/sync")
async def sync(request: web.Request) -> web.Response:
    """Run one upload pass and one download pass now, instead of on the timer.

    Delivery of what comes back stays with the bot's own loop, so a transcript
    is never posted twice by two callers racing.
    """
    bot = request.app[BOT]
    uploader = getattr(bot, "uploader", None)
    fetcher = getattr(bot, "fetcher", None)
    if uploader is None and fetcher is None:
        raise ApiError(
            409, "conflict", "Transcripts move through a shared folder; nothing to sync."
        )
    lock: asyncio.Lock = request.app[SYNC_LOCK]
    if lock.locked():
        raise ApiError(409, "conflict", "A sync is already running.")
    async with lock:
        uploaded = await uploader.run_once() if uploader is not None else []
        fetched = await fetcher.run_once() if fetcher is not None else []
    body = await _queue(request)
    return web.json_response({"uploaded": len(uploaded), "fetched": len(fetched), **body})
