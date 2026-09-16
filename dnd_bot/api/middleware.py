"""CORS, content-type guards and the single error shape.

Every failure leaves here as the same JSON envelope with a generic message.
Exception text in this codebase routinely carries filesystem paths and
occasionally configuration values - the same reasoning as the command error
handler in bot.py - so the detail goes to the log and the caller gets a code.
"""

from __future__ import annotations

import json
import logging
import re

from aiohttp import web

from . import schemas
from .keys import CONFIG

log = logging.getLogger(__name__)

# Applied to every response. None of this API is cacheable or embeddable, and
# saying so costs nothing.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    # The default advertises aiohttp and its exact version, which is only ever
    # useful to somebody looking for a matching advisory.
    "Server": "dnd-bot",
}

CORS_HEADERS = "authorization, content-type"
CORS_METHODS = "GET, POST, DELETE, OPTIONS"

# Exceptions the API knows how to answer. Anything else is a 500.
STATUS_BY_ERROR: dict[str, tuple[int, str]] = {
    "RecordingError": (409, "conflict"),
    "TrackResolutionError": (502, "resolver_failed"),
    "SourceDisabled": (503, "source_disabled"),
    "MusicError": (409, "conflict"),
}


class ApiError(Exception):
    """A deliberate, already-safe-to-show failure."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# RecordingError and MusicError carry operator-written text that is safe to
# show, but some of it interpolates an exception which can quote an absolute
# path (recorder.py does this when finalization fails). schemas.py promises no
# response carries a filesystem path, so this is where that promise is kept.
PATHS = re.compile(r"(?:[A-Za-z]:)?[\\/](?:[\w.-]+[\\/])+[\w.-]*")


def redact_paths(message: str) -> str:
    return PATHS.sub("<path>", message)


def json_error(status: int, code: str, message: str) -> web.Response:
    return web.json_response(schemas.error(code, redact_paths(message)), status=status)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Echo an exact allowlisted origin, never a wildcard.

    A wildcard plus a bearer token that can stop a recording is a combination
    worth making impossible rather than merely discouraged, so `*` is refused
    at config load and never constructed here.
    """
    origin = request.headers.get("Origin")
    allowed = origin if origin in request.app[CONFIG].api_cors_origins else None

    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)

    if allowed:
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Access-Control-Allow-Headers"] = CORS_HEADERS
        response.headers["Access-Control-Allow-Methods"] = CORS_METHODS
        response.headers["Access-Control-Max-Age"] = "600"
    # Vary regardless: the response differs by Origin even when it is refused,
    # and a cache that missed that would serve one site's answer to another.
    response.headers["Vary"] = "Origin"
    return response


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    response = await handler(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as exc:
        return json_error(exc.status, exc.code, exc.message)
    except web.HTTPException as exc:
        # aiohttp's own 404/405/413 arrive here as exceptions; they would
        # otherwise render as HTML, which no JSON client enjoys parsing.
        code = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}.get(
            exc.status, "error"
        )
        if exc.status >= 500:
            log.exception("Unhandled HTTP error on %s", request.path)
            return json_error(exc.status, "internal_error", "Something went wrong.")
        return json_error(exc.status, code, exc.reason or code.replace("_", " "))
    except TimeoutError:
        return json_error(504, "upstream_timeout", "The upstream source timed out.")
    except ValueError:
        # Not ApiError: an unplanned ValueError's text is not written for a
        # caller and may quote internals. The log keeps the detail.
        log.warning("Bad request on %s %s", request.method, request.path, exc_info=True)
        return json_error(400, "bad_request", "The request could not be understood.")
    except Exception as exc:  # noqa: BLE001 - the envelope is the point
        mapped = STATUS_BY_ERROR.get(type(exc).__name__)
        if mapped:
            status, code = mapped
            # These carry operator-written text (`/session start` shows the same
            # strings in Discord), so the message is safe to pass through.
            return json_error(status, code, str(exc))
        log.exception("Unhandled error on %s %s", request.method, request.path)
        return json_error(500, "internal_error", "Something went wrong.")


@web.middleware
async def body_middleware(request: web.Request, handler):
    """Refuse anything that is not JSON before a handler has to think about it."""
    if request.method in {"POST", "PUT", "PATCH"}:
        content_type = request.headers.get("Content-Type", "")
        if request.can_read_body and not content_type.startswith("application/json"):
            return json_error(415, "unsupported_media_type", "Send application/json.")
    return await handler(request)


async def read_json(request: web.Request) -> dict:
    """Parse a request body, rejecting anything that is not a JSON object.

    A `guild_id` in the body is refused outright: every route acts on the
    configured guild, so accepting one would only ever be an attempt to aim a
    stolen token somewhere else.
    """
    if not request.can_read_body:
        return {}
    try:
        payload = json.loads(await request.text())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "bad_request", "Body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ApiError(400, "bad_request", "Body must be a JSON object.")
    if "guild_id" in payload:
        raise ApiError(400, "bad_request", "guild_id is not accepted; the bot serves one guild.")
    return payload


def require_snowflake(payload: dict, key: str) -> int:
    raw = payload.get(key)
    if raw is None:
        raise ApiError(400, "bad_request", f"{key} is required.")
    try:
        return int(str(raw))
    except ValueError as exc:
        raise ApiError(400, "bad_request", f"{key} must be a numeric id.") from exc
