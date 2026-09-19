"""Campaigns over HTTP: CRUD, glossary, tagging a session, and no leaked ids."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import SESSION_START

from dnd_bot import paths
from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.db import Database
from dnd_bot.timeutil import to_iso

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


class FakeGuild:
    id = 1

    def get_member(self, user_id):
        return SimpleNamespace(id=user_id, display_name="Aylin") if user_id == 10 else None


@pytest.fixture
async def db(config: Config):
    config.ensure_dirs()
    database = Database(config.db_path, MIGRATIONS)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def client(config: Config, db: Database):
    guild = FakeGuild()
    bot = SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN),
        db=db,
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=None,
        is_ready=lambda: True,
        get_guild=lambda gid: guild if gid == 1 else None,
    )
    async with TestClient(TestServer(build_app(bot))) as test_client:
        test_client.bot = bot
        yield test_client


async def post(client, path, body):
    return await client.post(path, json=body, headers=AUTH)


async def seed_session(db: Database, session_id="s1", completed=True) -> None:
    await db.create_session(
        session_id=session_id,
        name="Test",
        guild_id=1,
        channel_id=2,
        channel_name="Table",
        text_channel_id=3,
        started_by_user_id=10,
        start_time=to_iso(SESSION_START),
        participants={"10": "Old"},
        language="tr",
        base_labels={"10": "Aylin"},
    )
    if completed:
        await db.update_session(
            session_id, completed=1, end_time=to_iso(SESSION_START.replace(hour=20))
        )


async def test_needs_the_token(client):
    assert (await client.get("/api/v1/campaigns")).status == 401


async def test_list_starts_empty(client):
    response = await client.get("/api/v1/campaigns", headers=AUTH)
    assert response.status == 200
    assert await response.json() == []


async def test_create_then_list_and_reject_duplicates(client):
    created = await post(client, "/api/v1/campaigns", {"name": "Strahd", "channel_id": "555"})
    assert created.status == 201
    body = await created.json()
    assert body["name"] == "Strahd"
    assert body["channel_id"] == "555"
    assert body["archived"] is False
    assert body["session_count"] == 0
    assert set(body) == {"id", "name", "channel_id", "language", "archived", "session_count"}

    listed = await (await client.get("/api/v1/campaigns", headers=AUTH)).json()
    assert [c["name"] for c in listed] == ["Strahd"]

    duplicate = await post(client, "/api/v1/campaigns", {"name": "strahd"})
    assert duplicate.status == 409


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "x" * 61},
        {"name": "ok", "language": "not a code"},
        {"name": "ok", "extra": 1},
    ],
)
async def test_create_validates_the_body(client, body):
    assert (await post(client, "/api/v1/campaigns", body)).status == 400


async def test_a_guild_id_is_refused(client):
    assert (await post(client, "/api/v1/campaigns", {"name": "a", "guild_id": "1"})).status == 400


async def test_detail_has_glossary_and_characters_without_user_ids(client, db):
    created = await (await post(client, "/api/v1/campaigns", {"name": "Strahd"})).json()
    await db.set_campaign_character(created["id"], 10, "Thorin")
    await db.set_campaign_character(created["id"], 99, "Ghost")
    await post(client, f"/api/v1/campaigns/{created['id']}/terms", {"terms": ["Eldrin"]})
    await post(
        client,
        f"/api/v1/campaigns/{created['id']}/corrections",
        {"corrections": [{"heard": "el drin", "correct": "Eldrin"}]},
    )

    response = await client.get(f"/api/v1/campaigns/{created['id']}", headers=AUTH)
    text = await response.text()
    body = json.loads(text)
    assert body["terms"] == ["Eldrin"]
    assert body["corrections"] == [{"heard": "el drin", "correct": "Eldrin"}]
    assert body["characters"] == [
        {"character_name": "Ghost", "member": None},
        {"character_name": "Thorin", "member": "Aylin"},
    ]
    assert "user_id" not in text
    assert '"10"' not in text and '"99"' not in text


async def test_unknown_campaign_is_404(client):
    assert (await client.get("/api/v1/campaigns/" + "a" * 12, headers=AUTH)).status == 404
    assert (await client.get("/api/v1/campaigns/not-an-id", headers=AUTH)).status == 404


async def test_update_renames_clears_channel_and_archives(client):
    created = await (
        await post(client, "/api/v1/campaigns", {"name": "A", "channel_id": "7"})
    ).json()
    url = f"/api/v1/campaigns/{created['id']}/update"
    renamed = await (await post(client, url, {"name": "B", "channel_id": None})).json()
    assert renamed["name"] == "B"
    assert renamed["channel_id"] is None
    archived = await (await post(client, url, {"archived": True})).json()
    assert archived["archived"] is True
    assert await (await client.get("/api/v1/campaigns", headers=AUTH)).json() == []
    listed = await (await client.get("/api/v1/campaigns?archived=1", headers=AUTH)).json()
    assert [c["name"] for c in listed] == ["B"]


@pytest.mark.parametrize("body", [{}, {"id": "x"}, {"archived": "yes"}, {"name": ""}])
async def test_update_validates(client, body):
    created = await (await post(client, "/api/v1/campaigns", {"name": "A"})).json()
    response = await post(client, f"/api/v1/campaigns/{created['id']}/update", body)
    assert response.status == 400


async def test_terms_and_corrections_limits(client):
    created = await (await post(client, "/api/v1/campaigns", {"name": "A"})).json()
    base = f"/api/v1/campaigns/{created['id']}"
    assert (await post(client, base + "/terms", {"terms": ["x", "y"]})).status == 200
    assert (await post(client, base + "/terms", {"terms": "nope"})).status == 400
    too_many = [{"heard": f"h{i}", "correct": "c"} for i in range(201)]
    assert (await post(client, base + "/corrections", {"corrections": too_many})).status == 400
    bad = {"corrections": [{"heard": "a"}]}
    assert (await post(client, base + "/corrections", bad)).status == 400


async def test_assign_and_unassign_a_session(client, db):
    created = await (await post(client, "/api/v1/campaigns", {"name": "Strahd"})).json()
    await seed_session(db)

    tagged = await post(client, "/api/v1/sessions/s1/campaign", {"campaign_id": created["id"]})
    assert tagged.status == 200
    body = await tagged.json()
    assert body["campaign_id"] == created["id"]
    assert body["campaign_name"] == "Strahd"

    cleared = await post(client, "/api/v1/sessions/s1/campaign", {"campaign_id": None})
    assert (await cleared.json())["campaign_id"] is None


async def test_assign_errors(client, db):
    created = await (await post(client, "/api/v1/campaigns", {"name": "Strahd"})).json()
    await seed_session(db)
    await seed_session(db, "live", completed=False)
    url = "/api/v1/sessions/{}/campaign"
    assert (await post(client, url.format("nope"), {"campaign_id": created["id"]})).status == 404
    assert (await post(client, url.format("s1"), {"campaign_id": "a" * 12})).status == 404
    assert (await post(client, url.format("live"), {"campaign_id": created["id"]})).status == 409
    assert (await post(client, url.format("s1"), {"campaign_id": 5})).status == 400
    assert (await post(client, url.format("s1"), {})).status == 400


async def test_sessions_list_filters_by_campaign(client, db):
    created = await (await post(client, "/api/v1/campaigns", {"name": "Strahd"})).json()
    await seed_session(db, "s1")
    await seed_session(db, "s2")
    await post(client, "/api/v1/sessions/s1/campaign", {"campaign_id": created["id"]})

    async def ids(query):
        response = await client.get("/api/v1/sessions" + query, headers=AUTH)
        assert response.status == 200
        return {row["id"] for row in await response.json()}

    assert await ids("") == {"s1", "s2"}
    assert await ids(f"?campaign={created['id']}") == {"s1"}
    assert await ids("?campaign=unassigned") == {"s2"}
    assert (await client.get("/api/v1/sessions?campaign=x%3B--", headers=AUTH)).status == 400


async def test_transcript_is_served_through_the_campaign(client, db, config):
    created = await (await post(client, "/api/v1/campaigns", {"name": "Strahd"})).json()
    await db.set_campaign_character(created["id"], 10, "Thorin")
    await post(
        client,
        f"/api/v1/campaigns/{created['id']}/corrections",
        {"corrections": [{"heard": "el drin", "correct": "Eldrin"}]},
    )
    await seed_session(db)
    path = paths.transcript_json_path(config.sessions_dir, "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    segment = {"speaker": "Old", "user_id": "10", "start": 1, "end": 2, "text": "el drin"}
    path.write_text(json.dumps({"segments": [segment]}), encoding="utf-8")

    before = await (await client.get("/api/v1/sessions/s1/transcript", headers=AUTH)).json()
    assert before["segments"][0]["speaker"] == "Old"
    assert before["segments"][0]["text"] == "el drin"
    assert before["session"]["campaign_id"] is None

    await post(client, "/api/v1/sessions/s1/campaign", {"campaign_id": created["id"]})
    after = await (await client.get("/api/v1/sessions/s1/transcript", headers=AUTH)).json()
    assert after["segments"][0]["speaker"] == "Thorin"
    assert after["segments"][0]["text"] == "Eldrin"
    assert after["session"]["campaign_name"] == "Strahd"
    assert "user_id" not in json.dumps(after)
