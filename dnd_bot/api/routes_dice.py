"""Dice rolls made in the portal, posted to Discord.

The portal rolls; the bot only posts. Everything in the body is user text, so it
is length-capped, flattened to one line, markdown-escaped and sent with every
mention disabled.
"""

from __future__ import annotations

import logging
import re

import discord
from aiohttp import web

from .keys import BOT, CONFIG
from .middleware import ApiError, read_json

log = logging.getLogger(__name__)

routes = web.RouteTableDef()

# No backtick: the expression is shown inside an inline code span.
EXPRESSION = re.compile(r"^[0-9dDkKhHlL+\-% ]{1,100}$")
SNOWFLAKE = re.compile(r"^\d{17,20}$")
ALLOWED_KEYS = frozenset({"expression", "total", "breakdown", "label", "channel_id"})
MAX_TOTAL = 100_000


def _text(payload: dict, key: str, max_len: int, *, required: bool) -> str | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{key} must be text.")
    flat = " ".join(value.split())
    if not 1 <= len(flat) <= max_len or len(value) > max_len * 2:
        raise ApiError(400, "bad_request", f"{key} must be 1-{max_len} characters.")
    return flat


def parse_roll(payload: dict) -> dict:
    if set(payload) - ALLOWED_KEYS:
        raise ApiError(400, "bad_request", "Unknown field in body.")

    expression = payload.get("expression")
    if not isinstance(expression, str) or not EXPRESSION.match(expression):
        raise ApiError(400, "bad_request", "expression must be dice notation, 1-100 characters.")

    total = payload.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or abs(total) > MAX_TOTAL:
        raise ApiError(400, "bad_request", "total must be a whole number.")

    channel_id = payload.get("channel_id")
    if channel_id is not None and (
        not isinstance(channel_id, str) or not SNOWFLAKE.match(channel_id)
    ):
        raise ApiError(400, "bad_request", "channel_id must be a Discord id string.")

    return {
        "expression": " ".join(expression.split()),
        "total": total,
        "breakdown": _text(payload, "breakdown", 300, required=True),
        "label": _text(payload, "label", 80, required=False),
        "channel_id": int(channel_id) if channel_id else None,
    }


def format_message(roll: dict) -> str:
    escape = discord.utils.escape_markdown
    head = f"🎲 **{escape(roll['label'])}** " if roll["label"] else "🎲 "
    return (
        f"{head}`{roll['expression']}` → **{roll['total']}**\n"
        f"-# {escape(roll['breakdown'])} · rolled in the DM portal"
    )


@routes.post("/api/v1/dice/announce")
async def announce(request: web.Request) -> web.Response:
    roll = parse_roll(await read_json(request))
    config = request.app[CONFIG]
    channel_id = roll["channel_id"] or config.dice_channel_id
    if not channel_id:
        raise ApiError(409, "no_dice_channel", "No dice channel is set on the bot.")

    channel = request.app[BOT].get_channel(channel_id)
    guild = getattr(channel, "guild", None)
    if (
        channel is None
        or guild is None
        or guild.id != config.guild_id
        or not callable(getattr(channel, "send", None))
    ):
        raise ApiError(404, "channel_not_found", "That is not a text channel on this server.")

    try:
        await channel.send(format_message(roll), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        log.warning("Dice announce refused in channel %s (status %s)", channel_id, exc.status)
        raise ApiError(
            502, "discord_error", "Discord refused the message. Check the bot's permissions."
        ) from exc
    return web.json_response({"sent": True, "channel_id": str(channel_id)})
