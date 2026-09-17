"""Bearer auth and per-token rate limiting.

There is exactly one credential today, and it is equivalent to admin: it can
stop a live recording. Everything here is written so that adding Discord OAuth2
later is a second middleware building a richer Principal, with no handler
changes - hence the scopes, which the static token simply satisfies all of.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict
from dataclasses import dataclass, field

from aiohttp import web

from . import schemas
from .keys import CONFIG, RATE_LIMITER

# Paths served without a token. The healthcheck must answer before anyone is
# holding a credential, and it deliberately reveals nothing but liveness.
PUBLIC_PATHS = frozenset({"/api/v1/health"})

# Writes that reach a live recording or an external resolver are capped well
# below the general limit, whatever that is set to.
TIGHT_LIMIT_PREFIXES = ("/api/v1/recording/", "/api/v1/music/search", "/api/v1/music/upload")
TIGHT_LIMIT_PER_MINUTE = 10


@dataclass(frozen=True)
class Principal:
    """Who is calling. `user_id` stays None until OAuth2 lands."""

    kind: str
    user_id: str | None = None
    scopes: frozenset[str] = frozenset({"*"})

    def can(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


@dataclass
class RateLimiter:
    """Token buckets keyed on the caller, not the IP.

    Behind a reverse proxy every request arrives from the same address, so an
    IP-keyed limiter would either throttle everyone at once or nobody.
    """

    per_minute: int
    _hits: dict[tuple[str, str], list[float]] = field(default_factory=lambda: defaultdict(list))

    def check(self, key: str, bucket: str, limit: int, now: float | None = None) -> float | None:
        """Returns None when allowed, or the seconds to wait when not."""
        now = time.monotonic() if now is None else now
        recent = [stamp for stamp in self._hits[(key, bucket)] if now - stamp < 60.0]
        self._hits[(key, bucket)] = recent
        if len(recent) >= limit:
            return max(1.0, 60.0 - (now - recent[0]))
        recent.append(now)
        return None


def limit_for(path: str, general: int) -> tuple[str, int]:
    for prefix in TIGHT_LIMIT_PREFIXES:
        if path.startswith(prefix):
            return "tight", min(TIGHT_LIMIT_PER_MINUTE, general)
    return "general", general


def _unauthorized() -> web.Response:
    # No echo of what was sent: a rejected credential must not come back out in
    # a body that something downstream might log.
    return web.json_response(
        schemas.error("unauthorized", "A valid bearer token is required."),
        status=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.method == "OPTIONS" or request.path in PUBLIC_PATHS:
        return await handler(request)

    config = request.app[CONFIG]
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return _unauthorized()
    # compare_digest raises TypeError on non-ASCII str, which would surface as
    # a 500 and tell an attacker their input reached further than a bad token.
    try:
        matches = hmac.compare_digest(presented, config.api_token)
    except TypeError:
        return _unauthorized()
    if not matches:
        return _unauthorized()

    request["principal"] = Principal(kind="token")

    limiter: RateLimiter = request.app[RATE_LIMITER]
    bucket, limit = limit_for(request.path, config.api_rate_limit_per_minute)
    retry_after = limiter.check("token", bucket, limit)
    if retry_after is not None:
        return web.json_response(
            schemas.error("rate_limited", "Too many requests."),
            status=429,
            headers={"Retry-After": str(int(retry_after))},
        )
    return await handler(request)
