"""Renaming and deleting a finished session.

Deleting cannot be undone, so the body has to name the session it means. A
request that only carries an id in its path (a typo, a replayed URL, a script
looping over the wrong list) does nothing.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .. import session_admin
from ..session_admin import SessionBusy
from . import schemas
from .keys import BOT, CONFIG, SEARCH
from .middleware import ApiError, read_json

log = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


async def _finished_or_idle(request: web.Request) -> dict:
    """The session row, provided nothing is recording into it."""
    session_id = request.match_info["session_id"]
    if not session_admin.SESSION_ID.match(session_id):
        raise ApiError(404, "not_found", "No such session.")
    bot = request.app[BOT]
    row = await bot.db.get_session(session_id)
    if row is None:
        raise ApiError(404, "not_found", "No such session.")
    if any(active.session_id == session_id for active in bot.manager.active.values()):
        raise ApiError(409, "conflict", "That session is recording right now.")
    return row


@routes.post("/api/v1/sessions/{session_id}/update")
async def rename(request: web.Request) -> web.Response:
    row = await _finished_or_idle(request)
    payload = await read_json(request)
    if set(payload) != {"name"}:
        raise _bad('Send exactly {"name": <new name>}.')
    try:
        name = session_admin.normalize_title(payload["name"])
    except ValueError as exc:
        raise _bad(str(exc)) from exc
    db = request.app[BOT].db
    await db.update_session(row["id"], name=name)
    updated = await db.get_session(row["id"])
    return web.json_response(schemas.session_summary(updated))


@routes.post("/api/v1/sessions/{session_id}/delete")
async def delete(request: web.Request) -> web.Response:
    row = await _finished_or_idle(request)
    payload = await read_json(request)
    if set(payload) != {"confirm_id"} or payload["confirm_id"] != row["id"]:
        raise _bad('Send exactly {"confirm_id": <the session id>} to delete it.')

    bot = request.app[BOT]
    db = bot.db
    if await db.session_state(row["id"]) == "transcribing":
        raise ApiError(
            409,
            "conflict",
            "The transcriber is working on this session. Try again once it is done.",
        )

    try:
        removed = await asyncio.to_thread(
            session_admin.remove_files, request.app[CONFIG], getattr(bot, "store", None), row["id"]
        )
    except SessionBusy as exc:
        raise ApiError(409, "conflict", str(exc)) from exc
    except Exception:
        # Nothing has been deleted from the database, so the session is still
        # listed and the call can simply be repeated.
        log.exception("Could not remove the files of session %s", row["id"])
        raise ApiError(
            502,
            "delete_failed",
            "Some of its files could not be removed. Nothing was lost; try again.",
        ) from None

    await request.app[SEARCH].forget(row["id"])
    await db.delete_session(row["id"])
    return web.json_response(
        {
            "deleted": row["id"],
            "files_removed": removed.files,
            "bytes_freed": removed.bytes_freed,
            "remote_objects_removed": removed.remote_objects,
        }
    )
