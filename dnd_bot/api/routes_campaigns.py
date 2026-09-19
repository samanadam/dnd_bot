"""Campaigns: list, create, edit, glossary, and tagging a session with one.

The glossary and corrections are replaced wholesale rather than patched. The
portal edits them as a list, and a full replace has no ordering or partial-update
cases to get wrong.

No response here carries a Discord user id. Characters are reported as a
character name and the member's display name, nothing that could address them.
"""

from __future__ import annotations

import re

from aiohttp import web

from .. import campaigns as rules
from ..db import CampaignConflict
from . import schemas
from .keys import BOT, CONFIG
from .middleware import ApiError, read_json, require_snowflake

routes = web.RouteTableDef()

ID = re.compile(r"^[a-f0-9]{12}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
MAX_ACTIVE = 50
UPDATABLE = {"name", "channel_id", "language", "archived"}


def _db(request: web.Request):
    return request.app[BOT].db


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


async def _existing(request: web.Request) -> dict:
    campaign_id = request.match_info["campaign_id"]
    row = await _db(request).get_campaign(campaign_id) if ID.match(campaign_id) else None
    if row is None:
        raise ApiError(404, "not_found", "No such campaign.")
    return row


def _language(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not LANGUAGE.match(value):
        raise _bad("language must be a code such as 'tr' or 'en'.")
    return value


async def _detail(request: web.Request, row: dict) -> dict:
    db = _db(request)
    guild = request.app[BOT].get_guild(request.app[CONFIG].guild_id)
    characters = []
    stored = await db.campaign_characters(row["id"])
    for user_id, name in sorted(stored.items(), key=lambda item: item[1].lower()):
        member = guild.get_member(int(user_id)) if guild else None
        characters.append(
            {"character_name": name, "member": member.display_name if member else None}
        )
    counted = next(
        (c for c in await db.list_campaigns(include_archived=True) if c["id"] == row["id"]), row
    )
    return {
        **schemas.campaign(counted),
        "terms": await db.campaign_terms(row["id"]),
        "corrections": [
            {"heard": heard, "correct": correct}
            for heard, correct in await db.campaign_corrections(row["id"])
        ],
        "characters": characters,
    }


@routes.get("/api/v1/campaigns")
async def list_campaigns(request: web.Request) -> web.Response:
    rows = await _db(request).list_campaigns(include_archived=request.query.get("archived") == "1")
    return web.json_response([schemas.campaign(row) for row in rows])


@routes.post("/api/v1/campaigns")
async def create(request: web.Request) -> web.Response:
    payload = await read_json(request)
    unknown = set(payload) - {"name", "channel_id", "language"}
    if unknown:
        raise _bad(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    try:
        name = rules.normalize_name(payload.get("name"))
    except ValueError as exc:
        raise _bad(str(exc)) from exc
    language = _language(payload.get("language"))
    channel_id = require_snowflake(payload, "channel_id") if payload.get("channel_id") else None
    if len(await _db(request).list_campaigns()) >= MAX_ACTIVE:
        raise ApiError(409, "conflict", f"At most {MAX_ACTIVE} campaigns; archive one first.")
    try:
        row = await _db(request).create_campaign(
            name=name, channel_id=channel_id, language=language
        )
    except CampaignConflict as exc:
        raise ApiError(409, "conflict", str(exc)) from exc
    return web.json_response(schemas.campaign(row), status=201)


@routes.get("/api/v1/campaigns/{campaign_id}")
async def detail(request: web.Request) -> web.Response:
    return web.json_response(await _detail(request, await _existing(request)))


@routes.post("/api/v1/campaigns/{campaign_id}/update")
async def update(request: web.Request) -> web.Response:
    row = await _existing(request)
    payload = await read_json(request)
    if not payload or set(payload) - UPDATABLE:
        raise _bad("Send one or more of: name, channel_id, language, archived.")
    fields: dict = {}
    if "name" in payload:
        try:
            fields["name"] = rules.normalize_name(payload["name"])
        except ValueError as exc:
            raise _bad(str(exc)) from exc
    if "channel_id" in payload:
        fields["channel_id"] = (
            None if payload["channel_id"] is None else require_snowflake(payload, "channel_id")
        )
    if "language" in payload:
        fields["language"] = _language(payload["language"])
    if "archived" in payload:
        if not isinstance(payload["archived"], bool):
            raise _bad("archived must be true or false.")
        fields["archived"] = int(payload["archived"])
    try:
        updated = await _db(request).update_campaign(row["id"], **fields)
    except CampaignConflict as exc:
        raise ApiError(409, "conflict", str(exc)) from exc
    return web.json_response(schemas.campaign(updated))


@routes.post("/api/v1/campaigns/{campaign_id}/terms")
async def set_terms(request: web.Request) -> web.Response:
    row = await _existing(request)
    payload = await read_json(request)
    try:
        terms = rules.normalize_terms(payload.get("terms"))
    except ValueError as exc:
        raise _bad(str(exc)) from exc
    await _db(request).replace_terms(row["id"], terms)
    return web.json_response(await _detail(request, row))


@routes.post("/api/v1/campaigns/{campaign_id}/corrections")
async def set_corrections(request: web.Request) -> web.Response:
    row = await _existing(request)
    payload = await read_json(request)
    try:
        pairs = rules.normalize_corrections(payload.get("corrections"))
    except ValueError as exc:
        raise _bad(str(exc)) from exc
    await _db(request).replace_corrections(row["id"], pairs)
    return web.json_response(await _detail(request, row))


@routes.post("/api/v1/sessions/{session_id}/campaign")
async def assign(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    if not SESSION_ID.match(session_id):
        raise ApiError(404, "not_found", "No such session.")
    payload = await read_json(request)
    if set(payload) != {"campaign_id"}:
        raise _bad('Send exactly {"campaign_id": <id or null>}.')
    campaign_id = payload["campaign_id"]
    if campaign_id is not None and (not isinstance(campaign_id, str) or not ID.match(campaign_id)):
        raise _bad("campaign_id must be a campaign id or null.")
    db = _db(request)
    row = await db.get_session(session_id)
    if row is None:
        raise ApiError(404, "not_found", "No such session.")
    if not row["completed"]:
        raise ApiError(409, "conflict", "That session is still being recorded.")
    try:
        updated = await db.assign_session_campaign(session_id, campaign_id)
    except LookupError as exc:
        raise ApiError(404, "not_found", "No such campaign.") from exc
    return web.json_response(schemas.session_summary(updated))
