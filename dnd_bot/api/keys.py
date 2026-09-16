"""Typed aiohttp application keys.

Bare string keys warn in aiohttp 3.9+ and are a silent typo away from a None at
request time, so every shared object hangs off one of these instead.
"""

from __future__ import annotations

from aiohttp import web

BOT = web.AppKey("bot", object)
CONFIG = web.AppKey("config", object)
RATE_LIMITER = web.AppKey("rate_limiter", object)
UPTIME = web.AppKey("uptime", object)
