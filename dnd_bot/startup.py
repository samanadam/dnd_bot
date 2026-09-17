"""Startup and shutdown guards around the Discord client.

The client can log in and then never dispatch READY: a background task inside
the library (fetching default sounds, for one) dies on a Discord 500 and nothing
retries it. The bot then has no API and no heartbeat, and Docker does not restart
an unhealthy container. Exiting non-zero hands the restart to
`restart: unless-stopped`, which brings a fresh connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

READY_TIMEOUT_EXIT_CODE = 3


async def wait_until_ready(
    is_ready: Callable[[], bool], timeout: float, poll: float = 1.0
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if is_ready():
            return True
        await asyncio.sleep(poll)
    return is_ready()


async def supervise_ready(
    bot,  # noqa: ANN001 - anything with is_ready() and close()
    timeout: float,
    poll: float = 1.0,
    stop: Callable[[], Awaitable[None]] | None = None,
) -> int | None:
    """Stop the bot when READY never arrives; return the exit code to use.

    `stop` defaults to `bot.close`. The bot passes its own `shutdown`, which marks
    the stop as deliberate before closing the connection.
    """
    if await wait_until_ready(bot.is_ready, timeout, poll):
        return None
    log.critical(
        "Discord never reported ready within %.0fs; exiting so the container restarts",
        timeout,
    )
    await (stop or bot.close)()
    return READY_TIMEOUT_EXIT_CODE


async def start_until_stopped(
    start: Callable[[], Awaitable[None]], is_shutting_down: Callable[[], bool]
) -> None:
    try:
        await start()
    except RuntimeError as exc:
        # Shutdown closes the HTTP session while the library may be reconnecting,
        # and that reconnect then fails with this message. It is the expected end
        # of a stop, not a crash.
        if is_shutting_down() and "Session is closed" in str(exc):
            return
        raise
