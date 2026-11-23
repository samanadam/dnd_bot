"""Party notes: the portal mirrors a campaign's notes into one text channel.

The portal decides what to say and when; the bot only posts, edits and deletes
its own messages there, with every mention switched off. Messages people write
in the channel travel the other way, see cogs/partynotes.py.
"""

from __future__ import annotations

import logging
import re

import discord
from aiohttp import web

from ..initiative import clean_name
from .keys import BOT, CONFIG
from .middleware import ApiError, read_json

log = logging.getLogger(__name__)

routes = web.RouteTableDef()

SNOWFLAKE = re.compile(r"^\d{17,20}$")
MAX_TEXT = 1500
NO_MENTIONS = discord.AllowedMentions.none()


def _snowflake(payload: dict, key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, str) or not SNOWFLAKE.match(value):
        raise ApiError(400, "bad_request", f"{key} must be a Discord id string.")
    return int(value)


def _text(payload: dict) -> str:
    value = payload.get("text")
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise ApiError(400, "bad_request", f"text must be 1-{MAX_TEXT} characters.")
    return value.strip()


def _author(payload: dict) -> str:
    value = payload.get("author")
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", "author must be text.")
    cleaned = clean_name(" ".join(value[:320].split()))[:80]
    if not cleaned:
        raise ApiError(400, "bad_request", "author must be 1-80 characters.")
    return cleaned


def format_note(author: str, text: str) -> str:
    return f"**{discord.utils.escape_markdown(author)}:** {text}"


def _channel(request: web.Request, channel_id: int):
    channel = request.app[BOT].get_channel(channel_id)
    guild = getattr(channel, "guild", None)
    if (
        channel is None
        or guild is None
        or guild.id != request.app[CONFIG].guild_id
        or not callable(getattr(channel, "send", None))
    ):
        raise ApiError(404, "channel_not_found", "That is not a text channel on this server.")
    return channel


async def _own_message(request: web.Request, payload: dict):
    channel = _channel(request, _snowflake(payload, "channel_id"))
    try:
        message = await channel.fetch_message(_snowflake(payload, "message_id"))
    except discord.NotFound:
        return None
    except discord.HTTPException as exc:
        raise ApiError(502, "discord_error", "Discord refused the request.") from exc
    # Never touch what a person wrote, only what this bot posted.
    if message.author.id != request.app[BOT].user.id:
        raise ApiError(403, "forbidden", "That message was not posted by the bot.")
    return message


@routes.post("/api/v1/party-notes/post")
async def post_note(request: web.Request) -> web.Response:
    payload = await read_json(request)
    channel = _channel(request, _snowflake(payload, "channel_id"))
    try:
        message = await channel.send(
            format_note(_author(payload), _text(payload)), allowed_mentions=NO_MENTIONS
        )
    except discord.HTTPException as exc:
        log.warning("Party note refused in channel %s (status %s)", channel.id, exc.status)
        raise ApiError(
            502, "discord_error", "Discord refused the message. Check the bot's permissions."
        ) from exc
    return web.json_response({"message_id": str(message.id)})


@routes.post("/api/v1/party-notes/edit")
async def edit_note(request: web.Request) -> web.Response:
    payload = await read_json(request)
    message = await _own_message(request, payload)
    if message is None:
        raise ApiError(404, "message_not_found", "That message no longer exists.")
    try:
        await message.edit(
            content=format_note(_author(payload), _text(payload)), allowed_mentions=NO_MENTIONS
        )
    except discord.HTTPException as exc:
        raise ApiError(502, "discord_error", "Discord refused the edit.") from exc
    return web.json_response({"edited": True})


@routes.post("/api/v1/party-notes/delete")
async def delete_note(request: web.Request) -> web.Response:
    payload = await read_json(request)
    message = await _own_message(request, payload)
    if message is not None:
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            raise ApiError(502, "discord_error", "Discord refused the delete.") from exc
    return web.json_response({"deleted": True})
