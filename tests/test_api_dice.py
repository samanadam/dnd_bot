"""Dice rolls posted from the portal.

Everything in the body is text a browser typed, so the tests care most about
what reaches Discord: escaped markdown, no mentions, no line breaks smuggled in,
and only channels of the configured guild.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GUILD = 1
CHANNEL = 123456789012345678
OTHER = 223456789012345678

BODY = {"expression": "1d20+5", "total": 17, "breakdown": "[12] + 5", "label": "Attack"}


class FakeChannel:
    def __init__(self, channel_id=CHANNEL, guild_id=GUILD, fail=None):
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id)
        self.sent = []
        self.fail = fail

    async def send(self, content, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append((content, kwargs))


def make_bot(config, channels, dice_channel_id=CHANNEL):
    config.ensure_dirs()
    return SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN, dice_channel_id=dice_channel_id),
        db=SimpleNamespace(pending_count=lambda: 0),
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=None,
        is_ready=lambda: True,
        get_channel=lambda cid: channels.get(cid),
    )


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
async def client(config, channel):
    async with TestClient(TestServer(build_app(make_bot(config, {CHANNEL: channel})))) as c:
        yield c


async def test_announce_sends_escaped_message_without_mentions(client, channel):
    body = {**BODY, "label": "@everyone **bold**\nsecond line"}
    response = await client.post("/api/v1/dice/announce", json=body, headers=AUTH)
    assert response.status == 200
    assert await response.json() == {"sent": True, "channel_id": str(CHANNEL)}
    content, kwargs = channel.sent[0]
    assert "\\*\\*bold\\*\\*" in content
    assert "second line" in content and "\nsecond line" not in content
    assert "**17**" in content
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False


async def test_announce_without_label(client, channel):
    body = {k: v for k, v in BODY.items() if k != "label"}
    response = await client.post("/api/v1/dice/announce", json=body, headers=AUTH)
    assert response.status == 200
    assert channel.sent[0][0].startswith("🎲 `1d20+5`")


async def test_announce_requires_token(client):
    response = await client.post("/api/v1/dice/announce", json=BODY)
    assert response.status == 401


@pytest.mark.parametrize(
    "patch",
    [
        {"expression": ""},
        {"expression": "1d20; drop table"},
        {"expression": "1d20`"},
        {"expression": "x" * 101},
        {"total": "17"},
        {"total": True},
        {"total": 1.5},
        {"total": 10**9},
        {"breakdown": ""},
        {"breakdown": "x" * 301},
        {"label": "x" * 81},
        {"label": 5},
        {"channel_id": "abc"},
        {"channel_id": 123456789012345678},
        {"extra": 1},
    ],
)
async def test_announce_rejects_bad_bodies(client, channel, patch):
    response = await client.post("/api/v1/dice/announce", json={**BODY, **patch}, headers=AUTH)
    assert response.status == 400
    assert channel.sent == []


async def test_unknown_channel_is_404(client):
    body = {**BODY, "channel_id": "999999999999999999"}
    response = await client.post("/api/v1/dice/announce", json=body, headers=AUTH)
    assert response.status == 404


async def test_explicit_channel_is_used(config):
    other = FakeChannel(channel_id=OTHER)
    bot = make_bot(config, {CHANNEL: FakeChannel(), OTHER: other})
    async with TestClient(TestServer(build_app(bot))) as c:
        body = {**BODY, "channel_id": str(OTHER)}
        response = await c.post("/api/v1/dice/announce", json=body, headers=AUTH)
        assert response.status == 200
        assert len(other.sent) == 1


async def test_channel_in_other_guild_is_404(config):
    foreign = FakeChannel(guild_id=2)
    async with TestClient(TestServer(build_app(make_bot(config, {CHANNEL: foreign})))) as c:
        response = await c.post("/api/v1/dice/announce", json=BODY, headers=AUTH)
        assert response.status == 404
        assert foreign.sent == []


async def test_non_text_channel_is_404(config):
    voice = SimpleNamespace(id=CHANNEL, guild=SimpleNamespace(id=GUILD))
    async with TestClient(TestServer(build_app(make_bot(config, {CHANNEL: voice})))) as c:
        response = await c.post("/api/v1/dice/announce", json=BODY, headers=AUTH)
        assert response.status == 404


async def test_no_default_channel_is_409(config):
    bot = make_bot(config, {}, dice_channel_id=None)
    async with TestClient(TestServer(build_app(bot))) as c:
        response = await c.post("/api/v1/dice/announce", json=BODY, headers=AUTH)
        assert response.status == 409


async def test_discord_refusal_is_502(client, channel):
    channel.fail = discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "no")
    response = await client.post("/api/v1/dice/announce", json=BODY, headers=AUTH)
    assert response.status == 502
