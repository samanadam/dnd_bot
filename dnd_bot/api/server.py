"""The aiohttp application and its runner.

It shares the bot's event loop on purpose: handlers touch VoiceClient objects
and SessionManager's per-key locks, and those are only safe on the loop that
owns them. AppRunner attaches to the running loop, which is exactly what is
wanted; a server that insisted on owning its own loop would need a thread and a
hand-off for every call.
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

from .auth import RateLimiter, auth_middleware
from .keys import BOT, CONFIG, RATE_LIMITER, UPTIME
from .middleware import (
    body_middleware,
    cors_middleware,
    error_middleware,
    security_headers_middleware,
)
from .routes_health import routes as health_routes

log = logging.getLogger(__name__)

# Bodies here are a handful of ids and a volume. Anything larger is a mistake or
# an attempt, and aiohttp rejects it before a handler allocates for it.
MAX_BODY_BYTES = 64 * 1024


def build_app(bot) -> web.Application:
    """Wire the app around an already-constructed bot.

    Takes the bot rather than reaching for a global so tests can hand it a stub
    with three attributes instead of a Discord connection.
    """
    app = web.Application(
        client_max_size=MAX_BODY_BYTES,
        middlewares=[
            # Order matters: CORS must wrap the error handler so a rejected
            # request still carries the headers a browser needs to read it,
            # and auth must run after the body guard so a 415 does not depend
            # on a credential.
            cors_middleware,
            security_headers_middleware,
            error_middleware,
            body_middleware,
            auth_middleware,
        ],
    )
    app[BOT] = bot
    app[CONFIG] = bot.config
    app[RATE_LIMITER] = RateLimiter(per_minute=bot.config.api_rate_limit_per_minute)

    started = time.monotonic()
    app[UPTIME] = lambda: time.monotonic() - started

    app.add_routes(health_routes)
    try:
        from .routes_recording import routes as recording_routes

        app.add_routes(recording_routes)
    except ImportError:  # pragma: no cover - present from phase 2 onward
        pass
    try:
        from .routes_music import routes as music_routes

        app.add_routes(music_routes)
    except ImportError:  # pragma: no cover - present from phase 3 onward
        pass

    from .routes_dice import routes as dice_routes

    app.add_routes(dice_routes)

    # No catch-all OPTIONS route: cors_middleware answers preflights before the
    # handler runs, including for paths the router does not know. A catch-all
    # would also swallow unknown paths, turning every 404 into a 405.
    return app


class ApiServer:
    """Owns the aiohttp runner's lifetime, nothing else."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.config = bot.config
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        runner = web.AppRunner(
            build_app(self.bot),
            # A client that hangs up should not leave a handler running against
            # a live voice connection.
            handler_cancellation=True,
            access_log=None,
        )
        await runner.setup()
        site = web.TCPSite(runner, self.config.api_host, self.config.api_port)
        await site.start()
        self._runner = runner
        log.info("HTTP API listening on %s:%s", self.config.api_host, self.config.api_port)

    async def stop(self) -> None:
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
        log.info("HTTP API stopped")
