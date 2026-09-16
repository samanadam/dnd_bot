"""Start, stop, cancel and recover a recording over HTTP.

These are the routes that can lose a game, so they are deliberately thin: every
one resolves a channel, calls the same SessionManager method a slash command
would, and lets RecordingError become a 409. No recording logic lives here.
"""

from __future__ import annotations

import logging

import discord
from aiohttp import web

from . import schemas
from .keys import BOT, CONFIG
from .middleware import ApiError, read_json, require_snowflake

log = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _guild(request: web.Request):
    bot = request.app[BOT]
    guild = bot.get_guild(request.app[CONFIG].guild_id)
    if guild is None:
        raise ApiError(503, "not_ready", "The bot is not connected to its guild yet.")
    return guild


def _voice_channel(request: web.Request, channel_id: int):
    channel = _guild(request).get_channel(channel_id)
    if channel is None:
        raise ApiError(404, "not_found", "No such channel in this server.")
    if isinstance(channel, discord.abc.GuildChannel) and not isinstance(
        channel, discord.VoiceChannel
    ):
        raise ApiError(400, "bad_request", "That channel is not a voice channel.")
    return channel


def _invoker(request: web.Request, guild):
    """Who the session is recorded as having been started by.

    SessionManager.start only reads `invoker.id`. The API has no user of its
    own until OAuth lands, so a portal-started session is attributed to the
    configured admin, or to the bot itself when there is none.
    """
    admin_id = request.app[CONFIG].admin_user_id
    if admin_id is not None:
        member = guild.get_member(admin_id)
        if member is not None:
            return member
    return guild.me


def _stop_result(result) -> dict:
    return {
        "session_id": result.session_id,
        "name": result.name,
        "duration_seconds": result.duration_seconds,
        "speakers": list(result.speakers),
        "warnings": list(result.warnings),
        "enqueued": result.enqueued,
    }


@routes.get("/api/v1/recording")
async def list_recordings(request: web.Request) -> web.Response:
    manager = request.app[BOT].manager
    return web.json_response(
        [schemas.active_session(session) for session in manager.active.values()]
    )


@routes.post("/api/v1/recording/start")
async def start(request: web.Request) -> web.Response:
    payload = await read_json(request)
    channel = _voice_channel(request, require_snowflake(payload, "channel_id"))

    text_channel_id = payload.get("text_channel_id")
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise ApiError(400, "bad_request", "name must be a string.")

    session = await request.app[BOT].manager.start(
        channel=channel,
        text_channel_id=int(text_channel_id) if text_channel_id else None,
        invoker=_invoker(request, channel.guild),
        name=name,
    )
    log.info("Recording %s started via the API", session.session_id)
    return web.json_response(schemas.active_session(session), status=201)


@routes.post("/api/v1/recording/stop")
async def stop(request: web.Request) -> web.Response:
    payload = await read_json(request)
    channel_id = require_snowflake(payload, "channel_id")
    result = await request.app[BOT].manager.stop(
        request.app[CONFIG].guild_id, channel_id, reason="api"
    )
    if result is None:
        raise ApiError(404, "not_found", "Nothing is being recorded in that channel.")
    log.info("Recording %s stopped via the API", result.session_id)
    return web.json_response(_stop_result(result))


@routes.post("/api/v1/recording/cancel")
async def cancel(request: web.Request) -> web.Response:
    """Discards the audio. The portal must confirm before calling this."""
    payload = await read_json(request)
    channel_id = require_snowflake(payload, "channel_id")
    session_id = await request.app[BOT].manager.cancel(request.app[CONFIG].guild_id, channel_id)
    if session_id is None:
        raise ApiError(404, "not_found", "Nothing is being recorded in that channel.")
    log.info("Recording %s cancelled via the API; audio discarded", session_id)
    return web.json_response({"session_id": session_id})


@routes.post("/api/v1/recording/recover")
async def recover(request: web.Request) -> web.Response:
    payload = await read_json(request)
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ApiError(400, "bad_request", "session_id is required.")
    result = await request.app[BOT].manager.recover(session_id)
    return web.json_response(_stop_result(result))
