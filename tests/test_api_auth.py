"""The middleware stack: bearer auth, CORS, body limits, rate limiting.

These run against the real aiohttp app with a stub bot, because the parts worth
testing here are the ones aiohttp itself drives - preflights, content types and
the 401 shape - not our handlers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app
from dnd_bot.config import Config

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeDatabase:
    async def pending_count(self) -> int:
        return 0

    async def list_open_sessions(self) -> list[dict]:
        return []

    async def list_sessions(self, limit: int = 25) -> list[dict]:
        return []


def make_bot(config: Config, **overrides):
    defaults = {
        "config": config,
        "db": FakeDatabase(),
        "manager": SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        "store": None,
        "music": None,
        "is_ready": lambda: True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def api_config(config: Config) -> Config:
    from dataclasses import replace

    return replace(
        config,
        api_enabled=True,
        api_token=TOKEN,
        api_cors_origins=("https://portal.example",),
        api_rate_limit_per_minute=60,
    )


@pytest.fixture
async def client(api_config: Config):
    api_config.ensure_dirs()
    server = TestServer(build_app(make_bot(api_config)))
    async with TestClient(server) as client:
        yield client


# -- bearer ------------------------------------------------------------------


async def test_health_needs_no_token(client):
    """The healthcheck must answer before anyone is holding a credential."""
    response = await client.get("/api/v1/health")
    assert response.status == 200
    assert (await response.json())["status"] == "ok"


async def test_stats_without_a_token_is_401(client):
    response = await client.get("/api/v1/stats")
    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert (await response.json())["error"]["code"] == "unauthorized"


async def test_a_wrong_token_is_401(client):
    response = await client.get("/api/v1/stats", headers={"Authorization": "Bearer wrong"})
    assert response.status == 401


async def test_a_malformed_authorization_header_is_401(client):
    response = await client.get("/api/v1/stats", headers={"Authorization": TOKEN})
    assert response.status == 401


async def test_the_right_token_gets_through(client):
    assert (await client.get("/api/v1/stats", headers=AUTH)).status == 200


async def test_the_token_is_never_echoed_back(client):
    """A 401 body that repeated the attempt would hand it to anything logging."""
    response = await client.get("/api/v1/stats", headers={"Authorization": "Bearer hunter2"})
    assert "hunter2" not in await response.text()


# -- CORS --------------------------------------------------------------------


async def test_an_allowed_origin_gets_cors_headers(client):
    response = await client.options(
        "/api/v1/stats",
        headers={
            "Origin": "https://portal.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status == 204
    assert response.headers["Access-Control-Allow-Origin"] == "https://portal.example"
    assert "authorization" in response.headers["Access-Control-Allow-Headers"].lower()
    assert response.headers["Vary"] == "Origin"


async def test_an_unknown_origin_gets_no_cors_headers(client):
    response = await client.options(
        "/api/v1/stats",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "Access-Control-Allow-Origin" not in response.headers


async def test_cors_is_never_a_wildcard(client):
    response = await client.get("/api/v1/health", headers={"Origin": "https://portal.example"})
    assert response.headers.get("Access-Control-Allow-Origin") != "*"


# -- request shape -----------------------------------------------------------


async def test_a_non_json_body_is_415(client):
    response = await client.post(
        "/api/v1/recording/stop",
        data="channel_id=2",
        headers={**AUTH, "Content-Type": "text/plain"},
    )
    assert response.status == 415


async def test_an_oversized_body_is_413(client):
    response = await client.post(
        "/api/v1/recording/stop",
        data="x" * (128 * 1024),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status == 413


async def test_an_unknown_route_is_a_json_404(client):
    response = await client.get("/api/v1/nope", headers=AUTH)
    assert response.status == 404
    assert (await response.json())["error"]["code"] == "not_found"


# -- rate limiting -----------------------------------------------------------


async def test_a_burst_is_rate_limited(api_config):
    from dataclasses import replace

    tight = replace(api_config, api_rate_limit_per_minute=3)
    tight.ensure_dirs()
    server = TestServer(build_app(make_bot(tight)))
    async with TestClient(server) as client:
        statuses = [(await client.get("/api/v1/stats", headers=AUTH)).status for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[-1] == 429


async def test_a_rate_limited_response_says_when_to_retry(api_config):
    from dataclasses import replace

    tight = replace(api_config, api_rate_limit_per_minute=1)
    tight.ensure_dirs()
    server = TestServer(build_app(make_bot(tight)))
    async with TestClient(server) as client:
        await client.get("/api/v1/stats", headers=AUTH)
        response = await client.get("/api/v1/stats", headers=AUTH)
    assert response.status == 429
    assert int(response.headers["Retry-After"]) >= 1


async def test_health_is_not_rate_limited(api_config):
    """A throttled healthcheck would restart a perfectly good container."""
    from dataclasses import replace

    tight = replace(api_config, api_rate_limit_per_minute=1)
    tight.ensure_dirs()
    server = TestServer(build_app(make_bot(tight)))
    async with TestClient(server) as client:
        statuses = [(await client.get("/api/v1/health")).status for _ in range(4)]
    assert statuses == [200, 200, 200, 200]


# -- response hardening ------------------------------------------------------


async def test_responses_carry_hardening_headers(client):
    response = await client.get("/api/v1/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    # Session state and disk figures must not sit in an intermediary's cache.
    assert "no-store" in response.headers["Cache-Control"]


async def test_the_public_health_route_does_not_disclose_the_version(client):
    """Anyone can reach /health. A version number only helps somebody else."""
    body = await (await client.get("/api/v1/health")).json()
    assert "version" not in body
    assert body["status"] == "ok"


async def test_the_version_is_available_to_an_authenticated_caller(client):
    body = await (await client.get("/api/v1/stats", headers=AUTH)).json()
    assert body["version"]


async def test_a_server_header_does_not_advertise_the_stack(client):
    response = await client.get("/api/v1/health")
    assert "aiohttp" not in response.headers.get("Server", "")
