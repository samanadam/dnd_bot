"""Renaming, trashing, restoring and permanently removing a session.

Deleting is two steps. A session is first moved to the trash, which only hides it
and can be undone. Removing it for good is a separate call that only accepts a
session already in the trash, and both calls that destroy or hide something must
name the session in the body. A request that only carries an id in its path (a
typo, a replayed URL, a script looping over the wrong list) does nothing.
"""

from __future__ import annotations

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


async def _existing(request: web.Request) -> dict:
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


async def _confirmed(request: web.Request, row: dict) -> None:
    payload = await read_json(request)
    if set(payload) != {"confirm_id"} or payload["confirm_id"] != row["id"]:
        raise _bad('Send exactly {"confirm_id": <the session id>}.')


def _purge_at(request: web.Request, row: dict) -> str | None:
    due = session_admin.purge_at(row, request.app[CONFIG].trash_retention_days)
    return due.isoformat() if due else None


@routes.get("/api/v1/sessions/trash")
async def trash_list(request: web.Request) -> web.Response:
    rows = await request.app[BOT].db.list_trashed_sessions()
    return web.json_response(
        [schemas.trashed_session(row, _purge_at(request, row)) for row in rows]
    )


@routes.post("/api/v1/sessions/{session_id}/update")
async def rename(request: web.Request) -> web.Response:
    row = await _existing(request)
    if row.get("deleted_at"):
        raise ApiError(404, "not_found", "No such session.")
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


@routes.post("/api/v1/sessions/{session_id}/trash")
async def trash(request: web.Request) -> web.Response:
    row = await _existing(request)
    if row.get("deleted_at"):
        raise ApiError(409, "conflict", "That session is already in the trash.")
    await _confirmed(request, row)
    await session_admin.trash(request.app[BOT].db, row["id"])
    updated = await request.app[BOT].db.get_session(row["id"])
    return web.json_response(schemas.trashed_session(updated, _purge_at(request, updated)))


@routes.post("/api/v1/sessions/{session_id}/restore")
async def restore(request: web.Request) -> web.Response:
    row = await _existing(request)
    if not row.get("deleted_at"):
        raise ApiError(409, "conflict", "That session is not in the trash.")
    await _confirmed(request, row)
    db = request.app[BOT].db
    await session_admin.restore(db, row["id"])
    return web.json_response(schemas.session_summary(await db.get_session(row["id"])))


@routes.post("/api/v1/sessions/{session_id}/purge")
async def purge(request: web.Request) -> web.Response:
    row = await _existing(request)
    if not row.get("deleted_at"):
        raise ApiError(409, "conflict", "Move it to the trash first.")
    await _confirmed(request, row)

    bot = request.app[BOT]
    if await bot.db.session_state(row["id"]) == "transcribing":
        raise ApiError(
            409,
            "conflict",
            "The transcriber is working on this session. Try again once it is done.",
        )
    try:
        removed = await session_admin.purge(
            bot.db,
            request.app[CONFIG],
            getattr(bot, "store", None),
            request.app[SEARCH],
            row["id"],
        )
    except SessionBusy as exc:
        raise ApiError(409, "conflict", str(exc)) from exc
    except Exception:
        # The database row is deleted last, so the session is still in the trash
        # and the call can simply be repeated.
        log.exception("Could not remove session %s", row["id"])
        raise ApiError(
            502,
            "delete_failed",
            "Some of its files could not be removed. Nothing was lost; try again.",
        ) from None
    return web.json_response(
        {
            "purged": row["id"],
            "files_removed": removed.files,
            "bytes_freed": removed.bytes_freed,
            "remote_objects_removed": removed.remote_objects,
        }
    )
