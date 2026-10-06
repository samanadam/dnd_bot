"""What players reported with /init, for the portal's tracker, and the roster
that lets `/init` roll for them.

Reports carry the reporting member's Discord id so the portal can put the total
on that player's own combatant instead of guessing from the name. The portal
already knows the ids of players who signed in; the id is never shown.
"""

from __future__ import annotations

import re

from aiohttp import web

from ..initiative import NAME_MAX, clean_name
from .keys import BOT
from .middleware import ApiError, read_json

routes = web.RouteTableDef()

SNOWFLAKE = re.compile(r"^\d{17,20}$")
CAMPAIGN = re.compile(r"^[a-f0-9]{12}$")
MAX_ROSTER = 20
BONUS_MIN, BONUS_MAX = -20, 40


def parse_roster(payload: dict) -> tuple[str | None, list[dict]]:
    if set(payload) - {"campaign_id", "entries"}:
        raise ApiError(400, "bad_request", "Unknown field in body.")
    campaign_id = payload.get("campaign_id")
    if campaign_id is not None and (
        not isinstance(campaign_id, str) or not CAMPAIGN.match(campaign_id)
    ):
        raise ApiError(400, "bad_request", "campaign_id must be a campaign id.")
    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ROSTER:
        raise ApiError(400, "bad_request", f"entries must be a list of at most {MAX_ROSTER}.")
    out: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"user_id", "label", "bonus"}:
            raise ApiError(400, "bad_request", "Each entry is {user_id, label, bonus}.")
        user_id = entry.get("user_id")
        if not isinstance(user_id, str) or not SNOWFLAKE.match(user_id):
            raise ApiError(400, "bad_request", "user_id must be a Discord id string.")
        raw = entry.get("label")
        label = clean_name(" ".join(raw[:200].split())) if isinstance(raw, str) else ""
        if not label:
            raise ApiError(400, "bad_request", f"label must be 1-{NAME_MAX} characters.")
        bonus = entry.get("bonus")
        if (
            isinstance(bonus, bool)
            or not isinstance(bonus, int)
            or not BONUS_MIN <= bonus <= BONUS_MAX
        ):
            raise ApiError(
                400, "bad_request", f"bonus must be a whole number from {BONUS_MIN} to {BONUS_MAX}."
            )
        # One place per player: the active sheet.
        out[user_id] = {"user_id": user_id, "label": label, "bonus": bonus}
    return campaign_id, list(out.values())


@routes.get("/api/v1/initiative")
async def list_reports(request: web.Request) -> web.Response:
    rows = await request.app[BOT].db.list_initiative()
    return web.json_response(
        [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "label": row["label"],
                "value": row["value"],
                "source": row["source"],
                "at": row["created_at"],
            }
            for row in rows
        ]
    )


@routes.post("/api/v1/initiative/clear")
async def clear(request: web.Request) -> web.Response:
    """Drop one report, or every report when no id is given."""
    payload = await read_json(request)
    if set(payload) - {"id"}:
        raise ApiError(400, "bad_request", 'Send {} or {"id": <report id>}.')
    report_id = payload.get("id")
    if report_id is not None and (
        isinstance(report_id, bool) or not isinstance(report_id, int) or report_id < 1
    ):
        raise ApiError(400, "bad_request", "id must be a positive integer.")
    removed = await request.app[BOT].db.clear_initiative(report_id)
    return web.json_response({"removed": removed})


@routes.post("/api/v1/initiative/roster")
async def set_roster(request: web.Request) -> web.Response:
    """Replace the roster with the players of the battle now shown to them."""
    campaign_id, entries = parse_roster(await read_json(request))
    await request.app[BOT].db.set_initiative_roster(campaign_id, entries)
    return web.json_response({"count": len(entries)})


@routes.post("/api/v1/initiative/roster/clear")
async def clear_roster(request: web.Request) -> web.Response:
    payload = await read_json(request)
    if payload:
        raise ApiError(400, "bad_request", "This endpoint takes no body.")
    await request.app[BOT].db.clear_initiative_roster()
    return web.json_response({"cleared": True})
