"""/health, /stats and /sessions over a real database.

The leak test is the important one here: these responses are the ones a portal
renders, and a session row is full of Discord user ids.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import SESSION_START

from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.db import Database
from dnd_bot.timeutil import to_iso

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


class FakeVoiceClient:
    def is_connected(self) -> bool:
        return True


def fake_active_session(session_row: dict):
    from dnd_bot.recorder import ActiveSession
    from dnd_bot.timeutil import from_iso

    return ActiveSession(
        session_id="live-1",
        name="Live Game",
        guild_id=1,
        channel_id=2,
        channel_name="Table",
        text_channel_id=3,
        started_by_user_id="10",
        start_time=from_iso(session_row["start_time"]),
        voice_client=FakeVoiceClient(),
        sink=None,
        labels={"10": "Thorin", "11": "Elenya"},
        offsets={"10": 0.0, "11": 12.5},
    )


async def seed(db: Database, session_id: str = "session-1") -> None:
    """One finished session with two speakers, matching the session_row fixture."""
    await db.create_session(
        session_id=session_id,
        name="Test Session",
        guild_id=1,
        channel_id=2,
        channel_name="Table",
        text_channel_id=3,
        started_by_user_id=10,
        start_time=to_iso(SESSION_START),
        participants={"10": "Thorin", "11": "Elenya"},
        language="tr",
    )
    await db.update_session(
        session_id,
        completed=1,
        end_time=to_iso(SESSION_START.replace(hour=20)),
    )


@pytest.fixture
async def db(config: Config):
    config.ensure_dirs()
    database = Database(config.db_path, MIGRATIONS)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def api_config(config: Config) -> Config:
    return replace(config, api_enabled=True, api_token=TOKEN)


@pytest.fixture
async def client(api_config: Config, db: Database):
    bot = SimpleNamespace(
        config=api_config,
        db=db,
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=None,
        is_ready=lambda: True,
    )
    async with TestClient(TestServer(build_app(bot))) as client:
        client.bot = bot
        yield client


# -- health ------------------------------------------------------------------


async def test_health_reports_readiness_and_uptime(client):
    body = await (await client.get("/api/v1/health")).json()
    assert body["ready"] is True
    assert body["uptime_seconds"] >= 0


async def test_health_still_answers_when_the_database_is_broken(client):
    """A healthcheck that 500s on a bad database restarts the container mid-session."""

    class BrokenDatabase:
        async def pending_count(self):
            raise RuntimeError("database is locked")

    client.bot.db = BrokenDatabase()
    response = await client.get("/api/v1/health")
    assert response.status == 200
    body = await response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "unreachable"


# -- stats -------------------------------------------------------------------


async def test_stats_counts_pending_transcriptions(client, db):
    await seed(db)
    await db.mark_exported("session-1")
    body = await (await client.get("/api/v1/stats", headers=AUTH)).json()
    assert body["sessions"]["pending_transcription"] == 1
    assert body["disk"]["free_mb"] > 0


async def test_stats_lists_live_sessions(client, session_row):
    session = fake_active_session(session_row)
    client.bot.manager.active = {(1, 2): session}
    body = await (await client.get("/api/v1/stats", headers=AUTH)).json()
    active = body["sessions"]["active"]
    assert len(active) == 1
    assert active[0]["session_id"] == "live-1"
    assert active[0]["channel_id"] == "2"
    assert active[0]["elapsed_seconds"] > 0


async def test_stats_reports_the_storage_backend(client):
    body = await (await client.get("/api/v1/stats", headers=AUTH)).json()
    assert body["storage"]["backend"] == "local"


# -- sessions ----------------------------------------------------------------


async def test_sessions_lists_finished_sessions(client, db):
    await seed(db)
    body = await (await client.get("/api/v1/sessions", headers=AUTH)).json()
    assert [row["id"] for row in body] == ["session-1"]
    assert body[0]["speaker_count"] == 2
    assert body[0]["duration_seconds"] == 7200.0


async def test_sessions_honours_a_limit(client, db):
    for index in range(3):
        await seed(db, f"s{index}")
    body = await (await client.get("/api/v1/sessions?limit=2", headers=AUTH)).json()
    assert len(body) == 2


async def test_a_nonsense_limit_is_400(client):
    assert (await client.get("/api/v1/sessions?limit=abc", headers=AUTH)).status == 400
    assert (await client.get("/api/v1/sessions?limit=0", headers=AUTH)).status == 400


# -- the leak test -----------------------------------------------------------


async def test_no_response_carries_a_discord_user_id(client, db, session_row):
    """`10` and `11` are the fixture's speaker ids. They must never ship."""
    await seed(db)
    client.bot.manager.active = {(1, 2): fake_active_session(session_row)}

    for path in ("/api/v1/stats", "/api/v1/sessions"):
        body = await (await client.get(path, headers=AUTH)).json()
        text = json.dumps(body)
        assert "started_by_user_id" not in text, path
        assert "participants_json" not in text, path
        assert "offsets_json" not in text, path
        assert '"10"' not in text, path
        assert '"11"' not in text, path
        # The labels themselves are fine - they are what a human reads.
        assert "Thorin" in text, path
