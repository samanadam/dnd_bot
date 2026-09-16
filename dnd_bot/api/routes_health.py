"""Liveness, stats and the finished-session list. All read-only."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from ..cleanup import directory_size_bytes, free_space_mb
from . import schemas
from .keys import BOT, CONFIG, STORAGE_REACHABLE, UPTIME
from .middleware import ApiError

log = logging.getLogger(__name__)

routes = web.RouteTableDef()

MAX_SESSION_LIMIT = 200


@routes.get("/api/v1/health")
async def health(request: web.Request) -> web.Response:
    """Answers even when the database is gone.

    The container healthcheck does not read this - it reads the heartbeat file,
    because recording liveness is what should decide a restart - but the portal
    does, and a monitor that 500s the moment SQLite locks is worse than useless.
    """
    bot = request.app[BOT]
    db_ok = True
    try:
        await bot.db.pending_count()
    except Exception:  # noqa: BLE001 - degraded is a status, not a failure
        log.warning("Health check could not reach the database", exc_info=True)
        db_ok = False

    return web.json_response(
        schemas.health(
            ready=bool(bot.is_ready()),
            uptime_seconds=request.app[UPTIME](),
            db_ok=db_ok,
        )
    )


@routes.get("/api/v1/stats")
async def stats(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    config = request.app[CONFIG]

    active = [schemas.active_session(session) for session in bot.manager.active.values()]
    pending = await bot.db.pending_count()

    # Both walk the disk; directory_size_bytes walks the whole data tree, which
    # on a session-heavy host is thousands of files. Off the event loop, or the
    # heartbeat and the alone-timer stall behind a stats request.
    free_mb = await asyncio.to_thread(free_space_mb, config.data_dir)
    data_bytes = await asyncio.to_thread(directory_size_bytes, config.data_dir)

    music = bot.music.state_summary() if getattr(bot, "music", None) else None

    return web.json_response(
        schemas.stats(
            active=active,
            pending_transcription=pending,
            free_mb=round(free_mb, 1),
            data_bytes=data_bytes,
            low_disk=free_mb < config.disk_warning_threshold_mb,
            storage_backend=config.storage_backend,
            storage_reachable=request.app.get(STORAGE_REACHABLE),
            music=music,
        )
    )


@routes.get("/api/v1/sessions")
async def sessions(request: web.Request) -> web.Response:
    raw = request.query.get("limit", "25")
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "limit must be an integer.") from exc
    if not 1 <= limit <= MAX_SESSION_LIMIT:
        raise ApiError(400, "bad_request", f"limit must be between 1 and {MAX_SESSION_LIMIT}.")

    rows = await request.app[BOT].db.list_sessions(limit=limit)
    return web.json_response([schemas.session_summary(row) for row in rows])
