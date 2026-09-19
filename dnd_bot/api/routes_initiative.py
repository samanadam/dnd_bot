"""What players reported with /init, for the portal's tracker.

Reports carry a display name and a total, never the Discord user behind them.
"""

from __future__ import annotations

from aiohttp import web

from .keys import BOT
from .middleware import ApiError, read_json

routes = web.RouteTableDef()


@routes.get("/api/v1/initiative")
async def list_reports(request: web.Request) -> web.Response:
    rows = await request.app[BOT].db.list_initiative()
    return web.json_response(
        [
            {"id": row["id"], "label": row["label"], "value": row["value"], "at": row["created_at"]}
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
