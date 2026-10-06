"""Turn pings: the portal tells the bot whose turn it is, the bot tells them.

The portal decides when a ping is due (an opt-in per campaign, throttled there).
The bot only posts: in a text channel of the configured guild, mentioning the
one member named and nobody else, with every piece of text escaped.
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
ALLOWED_KEYS = frozenset({"channel_id", "user_id", "character_name", "encounter_name", "round"})


def _snowflake(payload: dict, key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, str) or not SNOWFLAKE.match(value):
        raise ApiError(400, "bad_request", f"{key} must be a Discord id string.")
    return int(value)


def _name(payload: dict, key: str, max_len: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{key} must be text.")
    cleaned = clean_name(" ".join(value[: max_len * 4].split()))[:max_len]
    if not cleaned:
        raise ApiError(400, "bad_request", f"{key} must be 1-{max_len} characters.")
    return cleaned


def parse_turn(payload: dict) -> dict:
    if set(payload) - ALLOWED_KEYS:
        raise ApiError(400, "bad_request", "Unknown field in body.")
    round_number = payload.get("round")
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or not 1 <= round_number <= 10_000
    ):
        raise ApiError(400, "bad_request", "round must be a whole number from 1 to 10000.")
    return {
        "channel_id": _snowflake(payload, "channel_id"),
        "user_id": _snowflake(payload, "user_id"),
        "character_name": _name(payload, "character_name", 40),
        "encounter_name": _name(payload, "encounter_name", 80),
        "round": round_number,
    }


def format_turn(turn: dict, member) -> str:
    escape = discord.utils.escape_markdown
    head = f"⚔️ Round {turn['round']} · **{escape(turn['character_name'])}**, your turn"
    tail = f"\n-# {escape(turn['encounter_name'])}"
    return f"{head} {member.mention}{tail}" if member is not None else f"{head}{tail}"


@routes.post("/api/v1/turn/announce")
async def announce(request: web.Request) -> web.Response:
    turn = parse_turn(await read_json(request))
    config = request.app[CONFIG]
    channel = request.app[BOT].get_channel(turn["channel_id"])
    guild = getattr(channel, "guild", None)
    if (
        channel is None
        or guild is None
        or guild.id != config.guild_id
        or not callable(getattr(channel, "send", None))
    ):
        raise ApiError(404, "channel_not_found", "That is not a text channel on this server.")

    # Only a member of this guild is mentioned, and only that member may be pinged.
    member = guild.get_member(turn["user_id"]) if hasattr(guild, "get_member") else None
    allowed = discord.AllowedMentions(
        everyone=False, roles=False, users=[member] if member else False
    )
    try:
        await channel.send(format_turn(turn, member), allowed_mentions=allowed)
    except discord.HTTPException as exc:
        log.warning("Turn ping refused in channel %s (status %s)", turn["channel_id"], exc.status)
        raise ApiError(
            502, "discord_error", "Discord refused the message. Check the bot's permissions."
        ) from exc
    return web.json_response({"sent": True, "mentioned": member is not None})
