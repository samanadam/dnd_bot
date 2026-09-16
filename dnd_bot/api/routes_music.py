"""Music control: library, queue, transport.

Every route dispatches on an explicit `source` name rather than inspecting an
id, so a yt-dlp outage cannot change how an R2 request behaves.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..tracks import SourceDisabled
from .keys import BOT, CONFIG
from .middleware import ApiError, read_json

log = logging.getLogger(__name__)

routes = web.RouteTableDef()

MAX_BROWSE_LIMIT = 200


def _music(request: web.Request):
    music = getattr(request.app[BOT], "music", None)
    if music is None:
        raise ApiError(503, "music_disabled", "Music is turned off on this bot.")
    return music


def _guild_id(request: web.Request) -> int:
    return request.app[CONFIG].guild_id


def _source(request: web.Request, name: str):
    music = _music(request)
    source = music.sources.get(name)
    if source is None:
        raise SourceDisabled(f"No such music source: {name}.")
    return source


def _state(request: web.Request, status: int = 200) -> web.Response:
    return web.json_response(_music(request).state_summary(_guild_id(request)), status=status)


@routes.get("/api/v1/music/state")
async def state(request: web.Request) -> web.Response:
    return _state(request)


@routes.get("/api/v1/music/library")
async def library(request: web.Request) -> web.Response:
    source = _source(request, request.query.get("source", "r2"))
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError as exc:
        raise ApiError(400, "bad_request", "limit must be an integer.") from exc
    if not 1 <= limit <= MAX_BROWSE_LIMIT:
        raise ApiError(400, "bad_request", f"limit must be between 1 and {MAX_BROWSE_LIMIT}.")

    tracks = await source.browse(request.query.get("q") or None, limit)
    return web.json_response([track.to_dict() for track in tracks])


@routes.post("/api/v1/music/search")
async def search(request: web.Request) -> web.Response:
    """Separate from /library because searching reaches an external service."""
    payload = await read_json(request)
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ApiError(400, "bad_request", "query is required.")
    source = _source(request, payload.get("source", "youtube"))
    tracks = await source.browse(query.strip(), 10)
    return web.json_response([track.to_dict() for track in tracks])


@routes.post("/api/v1/music/play")
async def play(request: web.Request) -> web.Response:
    payload = await read_json(request)
    track_id = payload.get("id")
    if not isinstance(track_id, str) or not track_id:
        raise ApiError(400, "bad_request", "id is required.")
    position = payload.get("position", "end")

    source = _source(request, payload.get("source", "r2"))
    # Resolution can download a file or reach YouTube; both are awaited here so
    # a failure is reported as a resolver error, never as a playback conflict.
    track = await source.resolve(track_id)

    channel_id = payload.get("channel_id")
    await _music(request).play(
        _guild_id(request),
        track,
        channel_id=int(channel_id) if channel_id else None,
        position=position,
    )
    log.info("Playing %s (%s) via the API", track.title, track.source)
    return _state(request, status=202)


@routes.post("/api/v1/music/pause")
async def pause(request: web.Request) -> web.Response:
    await _music(request).pause(_guild_id(request))
    return _state(request)


@routes.post("/api/v1/music/resume")
async def resume(request: web.Request) -> web.Response:
    await _music(request).resume(_guild_id(request))
    return _state(request)


@routes.post("/api/v1/music/skip")
async def skip(request: web.Request) -> web.Response:
    await _music(request).skip(_guild_id(request))
    return _state(request)


@routes.post("/api/v1/music/stop")
async def stop(request: web.Request) -> web.Response:
    await _music(request).stop(_guild_id(request))
    return _state(request)


@routes.post("/api/v1/music/volume")
async def volume(request: web.Request) -> web.Response:
    payload = await read_json(request)
    try:
        level = float(payload.get("volume"))
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "bad_request", "volume must be a number between 0 and 2.") from exc
    await _music(request).set_volume(_guild_id(request), level)
    return _state(request)


@routes.post("/api/v1/music/loop")
async def loop(request: web.Request) -> web.Response:
    payload = await read_json(request)
    mode = payload.get("mode")
    if not isinstance(mode, str):
        raise ApiError(400, "bad_request", "mode is required.")
    _music(request).set_loop(_guild_id(request), mode)
    return _state(request)


@routes.delete("/api/v1/music/queue")
async def clear_queue(request: web.Request) -> web.Response:
    _music(request).clear_queue(_guild_id(request))
    return _state(request)


@routes.delete("/api/v1/music/queue/{index}")
async def remove_from_queue(request: web.Request) -> web.Response:
    try:
        index = int(request.match_info["index"])
    except ValueError as exc:
        raise ApiError(400, "bad_request", "index must be an integer.") from exc
    _music(request).remove(_guild_id(request), index)
    return _state(request)


@routes.post("/api/v1/music/queue/move")
async def move_in_queue(request: web.Request) -> web.Response:
    payload = await read_json(request)
    try:
        source_index = int(payload["from"])
        target_index = int(payload["to"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(400, "bad_request", "from and to must be integers.") from exc
    _music(request).move(_guild_id(request), source_index, target_index)
    return _state(request)


@routes.post("/api/v1/music/join")
async def join(request: web.Request) -> web.Response:
    payload = await read_json(request)
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ApiError(400, "bad_request", "channel_id is required.")
    await _music(request).join(_guild_id(request), int(channel_id))
    return _state(request)


@routes.post("/api/v1/music/leave")
async def leave(request: web.Request) -> web.Response:
    """Only ever hangs up a connection music made itself - see MusicManager."""
    await _music(request).detach(_guild_id(request), reason="api_leave")
    return _state(request)
