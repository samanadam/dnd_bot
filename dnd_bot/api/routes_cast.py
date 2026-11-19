"""Live casts: start and stop from the portal, and the browser's own socket.

A browser cannot send a bearer header on a WebSocket and must never hold the
bot token, so the portal asks for a single-use ticket (authenticated as usual)
and the browser redeems it as the first message on the socket. The socket route
is the one path outside bearer auth; it checks the Origin and the ticket itself.
"""

from __future__ import annotations

import logging

from aiohttp import WSMsgType, web

from ..cast import FIRST_MESSAGE_SECONDS, MAX_MESSAGE_BYTES, TICKET_SECONDS, CastError, valid_name
from ..music import Track
from .keys import BOT, CONFIG
from .middleware import ApiError, read_json

log = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _cast(request: web.Request):
    music = getattr(request.app[BOT], "music", None)
    cast = getattr(music, "cast", None)
    if cast is None:
        # Off means absent, not forbidden: nothing to probe for.
        raise ApiError(404, "not_found", "Not found.")
    return music, cast


@routes.post("/api/v1/cast/start")
async def start(request: web.Request) -> web.Response:
    music, cast = _cast(request)
    payload = await read_json(request)
    try:
        name = valid_name(payload.get("name"))
    except CastError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
    channel_id = payload.get("channel_id")
    if channel_id is not None and not (isinstance(channel_id, str) and channel_id.isdigit()):
        raise ApiError(400, "bad_request", "channel_id must be a numeric id.")

    session, ticket = cast.create(name)
    track = Track(id=session.id, title=f"Live: {name}", source="cast", uri=session.id)
    try:
        await music.play(
            request.app[CONFIG].guild_id,
            track,
            channel_id=int(channel_id) if channel_id else None,
            position="now",
        )
    except BaseException:
        cast.end(session)
        raise
    log.info("Cast %s started via the API", session.id)
    return web.json_response(
        {"cast_id": session.id, "ticket": ticket, "expires_in": int(TICKET_SECONDS)},
        status=202,
    )


@routes.post("/api/v1/cast/stop")
async def stop(request: web.Request) -> web.Response:
    _, cast = _cast(request)
    return web.json_response({"stopped": cast.stop()})


@routes.get("/api/v1/cast/ws")
async def socket(request: web.Request) -> web.StreamResponse:
    music, cast = _cast(request)
    origin = request.headers.get("Origin")
    if origin is None or origin not in request.app[CONFIG].api_cors_origins:
        raise ApiError(403, "forbidden", "Origin not allowed.")

    ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE_BYTES, heartbeat=None)
    await ws.prepare(request)
    try:
        first = await ws.receive(timeout=FIRST_MESSAGE_SECONDS)
    except TimeoutError:
        await ws.close(code=4001)
        return ws
    session = cast.redeem(first.data) if first.type == WSMsgType.TEXT else None
    if session is None:
        await ws.close(code=4003)
        return ws
    await ws.send_str("ready")
    await cast.serve(ws, session)
    return ws
