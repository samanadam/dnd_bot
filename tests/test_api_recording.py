"""Recording control over HTTP.

This is the part of the API that can lose a game: /recording/stop ends a live
capture and /recording/cancel deletes its audio. The tests here pin the failure
shapes - a conflict must read as 409, not as a 500 - because the confirmation
dialogs in the portal are built on them.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.recorder import RecordingError, StopResult

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeSession:
    def __init__(self, channel_id: int = 2):
        self.session_id = "live-1"
        self.name = "Live Game"
        self.guild_id = 1
        self.channel_id = channel_id
        self.channel_name = "Table"
        self.start_time = None
        self.labels = {"10": "Thorin"}
        self.warnings = []

    def elapsed_seconds(self) -> float:
        return 60.0


class FakeManager:
    def __init__(self):
        self.active = {}
        self.calls = []
        self.campaign_ids = []
        self.start_error: Exception | None = None
        self.stop_result: StopResult | None = None
        self.cancelled: str | None = None
        self.recover_error: Exception | None = None

    def sessions_in_guild(self, guild_id):
        return list(self.active.values())

    async def start(self, *, channel, text_channel_id, invoker, name, campaign_id=None):
        self.calls.append(("start", channel.id, text_channel_id, name, invoker.id))
        self.campaign_ids.append(campaign_id)
        if self.start_error:
            raise self.start_error
        session = FakeSession(channel.id)
        self.active[(1, channel.id)] = session
        return session

    async def stop(self, guild_id, channel_id, reason="manual"):
        self.calls.append(("stop", guild_id, channel_id, reason))
        return self.stop_result

    async def cancel(self, guild_id, channel_id):
        self.calls.append(("cancel", guild_id, channel_id))
        return self.cancelled

    async def recover(self, session_id):
        self.calls.append(("recover", session_id))
        if self.recover_error:
            raise self.recover_error
        return StopResult("s1", "Recovered", 60.0, ["Thorin"], [], True)


class FakeVoiceChannel:
    def __init__(self, channel_id: int = 2):
        self.id = channel_id
        self.name = "Table"
        self.guild = None


class FakeGuild:
    def __init__(self, channel):
        self.id = 1
        self.me = SimpleNamespace(id=999)
        self._channels = {channel.id: channel}
        channel.guild = self

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, user_id):
        return SimpleNamespace(id=user_id)


@pytest.fixture
def manager():
    return FakeManager()


@pytest.fixture
async def client(config: Config, manager: FakeManager):
    config.ensure_dirs()
    channel = FakeVoiceChannel()
    guild = FakeGuild(channel)
    bot = SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN, admin_user_id=None),
        db=SimpleNamespace(pending_count=lambda: 0),
        manager=manager,
        store=None,
        music=None,
        is_ready=lambda: True,
        get_guild=lambda gid: guild if gid == 1 else None,
    )
    async with TestClient(TestServer(build_app(bot))) as test_client:
        test_client.bot = bot
        test_client.guild = guild
        yield test_client


# -- listing -----------------------------------------------------------------


async def test_recording_lists_nothing_when_idle(client):
    assert await (await client.get("/api/v1/recording", headers=AUTH)).json() == []


async def test_recording_lists_a_live_session(client, manager):
    manager.active[(1, 2)] = FakeSession()
    body = await (await client.get("/api/v1/recording", headers=AUTH)).json()
    assert body[0]["session_id"] == "live-1"


# -- start -------------------------------------------------------------------


async def test_start_returns_201_and_the_session(client, manager):
    response = await client.post(
        "/api/v1/recording/start", json={"channel_id": "2", "name": "Session 12"}, headers=AUTH
    )
    assert response.status == 201
    assert (await response.json())["session_id"] == "live-1"
    assert manager.calls[0][:4] == ("start", 2, None, "Session 12")


async def test_start_refuses_an_unknown_channel(client):
    response = await client.post(
        "/api/v1/recording/start", json={"channel_id": "404"}, headers=AUTH
    )
    assert response.status == 404


async def test_start_requires_a_channel_id(client):
    response = await client.post("/api/v1/recording/start", json={}, headers=AUTH)
    assert response.status == 400


async def test_start_rejects_a_non_numeric_channel_id(client):
    response = await client.post(
        "/api/v1/recording/start", json={"channel_id": "lobby"}, headers=AUTH
    )
    assert response.status == 400


async def test_a_recording_error_is_a_409_carrying_its_message(client, manager):
    """The portal shows this text; it is what /session start would have said."""
    manager.start_error = RecordingError("Already recording in #Table.")
    response = await client.post("/api/v1/recording/start", json={"channel_id": "2"}, headers=AUTH)
    assert response.status == 409
    body = await response.json()
    assert body["error"]["code"] == "conflict"
    assert body["error"]["message"] == "Already recording in #Table."


async def test_a_guild_id_in_the_body_is_refused(client):
    """One guild, from config. A body naming another is a stolen token."""
    response = await client.post(
        "/api/v1/recording/start", json={"channel_id": "2", "guild_id": "77"}, headers=AUTH
    )
    assert response.status == 400


# -- stop and cancel ---------------------------------------------------------


async def test_stop_returns_the_result(client, manager):
    manager.stop_result = StopResult("live-1", "Live Game", 3600.0, ["Thorin"], [], True)
    response = await client.post("/api/v1/recording/stop", json={"channel_id": "2"}, headers=AUTH)
    assert response.status == 200
    body = await response.json()
    assert body["session_id"] == "live-1"
    assert body["duration_seconds"] == 3600.0
    assert body["enqueued"] is True
    assert body["speakers"] == ["Thorin"]


async def test_stop_on_an_idle_channel_is_404(client, manager):
    manager.stop_result = None
    response = await client.post("/api/v1/recording/stop", json={"channel_id": "2"}, headers=AUTH)
    assert response.status == 404


async def test_stop_records_that_the_api_asked(client, manager):
    """reason lands in the logs; api is how you tell it from a slash command."""
    manager.stop_result = StopResult("live-1", "Live Game", 1.0, [], [], True)
    await client.post("/api/v1/recording/stop", json={"channel_id": "2"}, headers=AUTH)
    assert manager.calls[0] == ("stop", 1, 2, "api")


async def test_cancel_returns_the_discarded_session_id(client, manager):
    manager.cancelled = "live-1"
    response = await client.post("/api/v1/recording/cancel", json={"channel_id": "2"}, headers=AUTH)
    assert response.status == 200
    assert (await response.json())["session_id"] == "live-1"


async def test_cancel_on_an_idle_channel_is_404(client):
    response = await client.post("/api/v1/recording/cancel", json={"channel_id": "2"}, headers=AUTH)
    assert response.status == 404


# -- recover -----------------------------------------------------------------


async def test_recover_finalizes_an_orphaned_session(client, manager):
    response = await client.post(
        "/api/v1/recording/recover", json={"session_id": "s1"}, headers=AUTH
    )
    assert response.status == 200
    assert (await response.json())["session_id"] == "s1"


async def test_recover_refuses_an_unknown_session(client, manager):
    manager.recover_error = RecordingError("No session found with that id.")
    response = await client.post(
        "/api/v1/recording/recover", json={"session_id": "nope"}, headers=AUTH
    )
    assert response.status == 409


async def test_recover_requires_a_session_id(client):
    assert (await client.post("/api/v1/recording/recover", json={}, headers=AUTH)).status == 400


# -- leak --------------------------------------------------------------------


async def test_no_recording_response_carries_a_user_id(client, manager):
    manager.active[(1, 2)] = FakeSession()
    text = await (await client.get("/api/v1/recording", headers=AUTH)).text()
    assert '"10"' not in text
    assert "started_by_user_id" not in text


async def test_start_passes_the_campaign_choice_to_the_manager(client, manager):
    response = await client.post(
        "/api/v1/recording/start",
        json={"channel_id": "2", "campaign_id": "abc123abc123"},
        headers=AUTH,
    )
    assert response.status == 201
    assert manager.campaign_ids == ["abc123abc123"]
    body = await response.json()
    assert body["campaign_id"] is None and body["campaign_name"] is None


async def test_start_rejects_a_non_string_campaign(client):
    response = await client.post(
        "/api/v1/recording/start", json={"channel_id": "2", "campaign_id": 5}, headers=AUTH
    )
    assert response.status == 400
