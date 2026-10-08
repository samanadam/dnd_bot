"""Endpoints the portal's player features call: guild roles and channels,
character names from sheets, sheet rolls, the initiative roster and `/init`
rolls, and turn pings.

Every one of these is called by the portal's server only. The tests care about
what they refuse and about what reaches Discord: one mention at most, and only
of the member named.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app
from dnd_bot.cogs.initiative import InitiativeCog
from dnd_bot.config import Config
from dnd_bot.db import Database
from dnd_bot.initiative import roll_initiative

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
GUILD = 1
CHANNEL = 123456789012345678
PLAYER = 323456789012345678
STRANGER = 423456789012345678


class FakeMember:
    def __init__(self, user_id):
        self.id = user_id
        self.mention = f"<@{user_id}>"


class FakeChannel:
    def __init__(self, channel_id=CHANNEL, guild=None, fail=None):
        self.id = channel_id
        self.guild = guild
        self.sent = []
        self.fail = fail
        self.name = "table"
        self.category = SimpleNamespace(name="Game")
        self.position = 0

    async def send(self, content, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append((content, kwargs))

    def permissions_for(self, _me):
        return SimpleNamespace(view_channel=True, send_messages=True)


class FakeGuild:
    def __init__(self):
        self.id = GUILD
        self.me = object()
        self.roles = [
            SimpleNamespace(
                id=GUILD,
                name="@everyone",
                position=0,
                managed=False,
                color=SimpleNamespace(value=0),
            ),
            SimpleNamespace(
                id=11,
                name="Players",
                position=2,
                managed=False,
                color=SimpleNamespace(value=0x3366FF),
            ),
            SimpleNamespace(
                id=12, name="DM", position=5, managed=False, color=SimpleNamespace(value=0)
            ),
            SimpleNamespace(
                id=13, name="Some Bot", position=4, managed=True, color=SimpleNamespace(value=0)
            ),
        ]
        hidden = FakeChannel(222222222222222222, self)
        hidden.permissions_for = lambda _me: SimpleNamespace(view_channel=True, send_messages=False)
        self.text_channels = [FakeChannel(CHANNEL, self), hidden]
        self.members = {PLAYER: FakeMember(PLAYER)}

    def get_member(self, user_id):
        return self.members.get(user_id)


@pytest.fixture
async def db(config: Config):
    config.ensure_dirs()
    database = Database(config.db_path, MIGRATIONS)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
async def client(config: Config, db: Database, guild):
    channels = {c.id: c for c in guild.text_channels}
    bot = SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN, dice_channel_id=CHANNEL),
        db=db,
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=None,
        is_ready=lambda: True,
        get_guild=lambda gid: guild if gid == GUILD else None,
        get_channel=lambda cid: channels.get(cid),
    )
    async with TestClient(TestServer(build_app(bot))) as test_client:
        yield test_client


# -- guild ---------------------------------------------------------------------


async def test_roles_leave_out_everyone_and_managed_roles(client):
    response = await client.get("/api/v1/guild/roles", headers=AUTH)
    assert response.status == 200
    assert await response.json() == [
        {"id": "12", "name": "DM", "color": 0, "position": 5},
        {"id": "11", "name": "Players", "color": 0x3366FF, "position": 2},
    ]
    assert (await client.get("/api/v1/guild/roles")).status == 401


async def test_channels_list_only_where_the_bot_can_post(client):
    body = await (await client.get("/api/v1/guild/channels", headers=AUTH)).json()
    assert body == [{"id": str(CHANNEL), "name": "table", "category": "Game"}]


async def test_guild_missing_answers_503(config, db):
    bot = SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN),
        db=db,
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=None,
        is_ready=lambda: True,
        get_guild=lambda gid: None,
    )
    async with TestClient(TestServer(build_app(bot))) as c:
        assert (await c.get("/api/v1/guild/roles", headers=AUTH)).status == 503


# -- character names -------------------------------------------------------------


async def test_sheet_names_the_character_in_its_campaign(client, db):
    campaign = await db.create_campaign(name="Ember")
    path = f"/api/v1/campaigns/{campaign['id']}/characters"
    response = await client.post(
        path, json={"user_id": str(PLAYER), "character_name": "  Aria\nWindwhisper "}, headers=AUTH
    )
    assert response.status == 200
    assert await db.campaign_characters(campaign["id"]) == {str(PLAYER): "Aria Windwhisper"}
    cleared = await client.post(f"{path}/clear", json={"user_id": str(PLAYER)}, headers=AUTH)
    assert cleared.status == 200
    assert await db.campaign_characters(campaign["id"]) == {}


async def test_character_routes_refuse_bad_input(client, db):
    campaign = await db.create_campaign(name="Ember")
    path = f"/api/v1/campaigns/{campaign['id']}/characters"
    for bad in (
        {"user_id": PLAYER, "character_name": "A"},
        {"user_id": "12", "character_name": "A"},
        {"user_id": str(PLAYER), "character_name": "   "},
        {"user_id": str(PLAYER), "character_name": "A", "extra": 1},
    ):
        assert (await client.post(path, json=bad, headers=AUTH)).status == 400, bad
    missing = await client.post(
        "/api/v1/campaigns/ffffffffffff/characters",
        json={"user_id": str(PLAYER), "character_name": "A"},
        headers=AUTH,
    )
    assert missing.status == 404
    assert (
        await client.post(path, json={"user_id": str(PLAYER), "character_name": "A"})
    ).status == 401


# -- sheet rolls -----------------------------------------------------------------


async def test_sheet_rolls_say_where_they_came_from(client, guild):
    body = {
        "expression": "1d20+7",
        "total": 19,
        "breakdown": "[12] + 7",
        "label": "Aria · Stealth",
        "origin": "sheet",
    }
    response = await client.post("/api/v1/dice/announce", json=body, headers=AUTH)
    assert response.status == 200
    content = guild.text_channels[0].sent[0][0]
    assert "rolled on a character sheet" in content and "DM portal" not in content
    assert (
        await client.post("/api/v1/dice/announce", json={**body, "origin": "bot"}, headers=AUTH)
    ).status == 400


# -- roster and /init ---------------------------------------------------------------


async def test_roster_replaces_and_clears(client, db):
    entries = [
        {"user_id": str(PLAYER), "label": "Aria", "bonus": 3},
        {"user_id": str(STRANGER), "label": "Bram", "bonus": -1},
    ]
    response = await client.post(
        "/api/v1/initiative/roster",
        json={"campaign_id": "0123456789ab", "entries": entries},
        headers=AUTH,
    )
    assert await response.json() == {"count": 2}
    assert (await db.roster_entry(PLAYER))["bonus"] == 3
    await client.post(
        "/api/v1/initiative/roster",
        json={"campaign_id": None, "entries": entries[1:]},
        headers=AUTH,
    )
    assert await db.roster_entry(PLAYER) is None
    assert (
        await client.post("/api/v1/initiative/roster/clear", json={}, headers=AUTH)
    ).status == 200
    assert await db.roster_entry(STRANGER) is None


async def test_roster_refuses_bad_entries(client):
    good = {"user_id": str(PLAYER), "label": "Aria", "bonus": 3}
    for bad in (
        {"entries": [good], "extra": 1},
        {"entries": [{**good, "bonus": 41}]},
        {"entries": [{**good, "bonus": True}]},
        {"entries": [{**good, "user_id": "1"}]},
        {"entries": [{**good, "label": ""}]},
        {"entries": [{**good, "hp": 3}]},
        {"entries": [good] * 21},
        {"campaign_id": "nope", "entries": [good]},
    ):
        assert (
            await client.post("/api/v1/initiative/roster", json=bad, headers=AUTH)
        ).status == 400, bad
    assert (
        await client.post("/api/v1/initiative/roster/clear", json={"x": 1}, headers=AUTH)
    ).status == 400


def test_roll_initiative_keeps_the_right_die():
    dice = iter([4, 17, 4, 17, 9])
    roll = lambda: next(dice)  # noqa: E731
    assert roll_initiative(3, "advantage", roll) == (20, "[4, 17 → 17] + 3")
    assert roll_initiative(3, "disadvantage", roll) == (7, "[4, 17 → 4] + 3")
    assert roll_initiative(-2, "normal", roll) == (7, "[9] - 2")
    with pytest.raises(ValueError):
        roll_initiative(0, "lucky")


async def test_init_without_a_total_rolls_from_the_roster(db):
    replies = []
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=PLAYER), respond=lambda text, **kw: _record(replies, text, kw)
    )
    cog = InitiativeCog(SimpleNamespace(db=db))
    await cog._roll(ctx, "normal")
    assert "No battle is waiting on you" in replies[-1][0]
    assert await db.list_initiative() == []

    await db.set_initiative_roster(None, [{"user_id": str(PLAYER), "label": "Aria", "bonus": 3}])
    await cog._roll(ctx, "advantage")
    text, kwargs = replies[-1]
    assert kwargs["ephemeral"] is True and "for **Aria**" in text
    [report] = await db.list_initiative()
    assert report["label"] == "Aria" and report["source"] == "rolled" and 4 <= report["value"] <= 23


async def _record(replies, text, kwargs):
    replies.append((text, kwargs))


# -- turn pings ----------------------------------------------------------------


TURN = {
    "channel_id": str(CHANNEL),
    "user_id": str(PLAYER),
    "character_name": "Aria",
    "encounter_name": "Ambush",
    "round": 3,
}


async def test_turn_ping_mentions_only_that_member(client, guild):
    response = await client.post(
        "/api/v1/turn/announce", json={**TURN, "character_name": "**Aria** @everyone"}, headers=AUTH
    )
    assert await response.json() == {"sent": True, "mentioned": True}
    content, kwargs = guild.text_channels[0].sent[0]
    assert content.startswith("⚔️ Round 3 · **\\*\\*Aria\\*\\* @everyone**, your turn <@")
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.roles is False
    assert [m.id for m in mentions.users] == [PLAYER]


async def test_turn_ping_without_a_member_mentions_nobody(client, guild):
    response = await client.post(
        "/api/v1/turn/announce", json={**TURN, "user_id": str(STRANGER)}, headers=AUTH
    )
    assert await response.json() == {"sent": True, "mentioned": False}
    content, kwargs = guild.text_channels[0].sent[0]
    assert "<@" not in content and kwargs["allowed_mentions"].users is False


async def test_turn_ping_refuses_bad_input_and_foreign_channels(client):
    for bad in (
        {**TURN, "round": 0},
        {**TURN, "round": True},
        {**TURN, "user_id": PLAYER},
        {**TURN, "character_name": ""},
        {**TURN, "extra": 1},
    ):
        assert (
            await client.post("/api/v1/turn/announce", json=bad, headers=AUTH)
        ).status == 400, bad
    assert (
        await client.post(
            "/api/v1/turn/announce", json={**TURN, "channel_id": "999999999999999999"}, headers=AUTH
        )
    ).status == 404
    assert (await client.post("/api/v1/turn/announce", json=TURN)).status == 401


async def test_turn_ping_reports_a_discord_refusal(client, guild):
    guild.text_channels[0].fail = discord.HTTPException(
        SimpleNamespace(status=403, reason="Forbidden"), "no"
    )
    assert (await client.post("/api/v1/turn/announce", json=TURN, headers=AUTH)).status == 502
