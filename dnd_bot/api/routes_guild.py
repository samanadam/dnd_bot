"""The configured guild's roles and text channels, for the portal's settings.

Read-only. Roles feed the portal's permission editor (who may do what is
decided there); channels feed the turn-ping channel picker. Neither answer
carries a member or a user id.
"""

from __future__ import annotations

from aiohttp import web

from .keys import BOT, CONFIG
from .middleware import ApiError

routes = web.RouteTableDef()


def _guild(request: web.Request):
    guild = request.app[BOT].get_guild(request.app[CONFIG].guild_id)
    if guild is None:
        raise ApiError(503, "guild_unavailable", "The bot is not connected to the server yet.")
    return guild


@routes.get("/api/v1/guild/roles")
async def roles(request: web.Request) -> web.Response:
    guild = _guild(request)
    out = []
    for role in guild.roles:
        # @everyone shares the guild's id; managed roles belong to integrations and bots.
        if role.id == guild.id or getattr(role, "managed", False):
            continue
        colour = getattr(role, "color", None)
        out.append(
            {
                "id": str(role.id),
                "name": str(role.name),
                "color": int(getattr(colour, "value", colour or 0) or 0),
                "position": int(role.position),
            }
        )
    out.sort(key=lambda r: (-r["position"], r["name"].lower()))
    return web.json_response(out)


@routes.get("/api/v1/guild/channels")
async def channels(request: web.Request) -> web.Response:
    """Text channels the bot may post in."""
    guild = _guild(request)
    me = getattr(guild, "me", None)
    out = []
    for channel in getattr(guild, "text_channels", []):
        permissions = channel.permissions_for(me) if me is not None else None
        if permissions is not None and not (permissions.view_channel and permissions.send_messages):
            continue
        category = getattr(channel, "category", None)
        out.append(
            {
                "id": str(channel.id),
                "name": str(channel.name),
                "category": str(category.name) if category is not None else None,
                "position": int(getattr(channel, "position", 0)),
            }
        )
    out.sort(key=lambda c: (c["category"] or "", c["position"], c["name"].lower()))
    return web.json_response([{k: v for k, v in c.items() if k != "position"} for c in out])
