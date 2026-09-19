"""Initiative reports: the rules, the storage, and the API."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.db import Database
from dnd_bot.initiative import Cooldown, clean_name, valid_value

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def test_names_are_one_short_printable_line():
    assert clean_name("  Thorin \n  Ironfist ") == "Thorin Ironfist"
    assert clean_name("a\x00b\x1b[31m") == "ab[31m"
    assert len(clean_name("x" * 90)) == 40
    assert clean_name("\u200b") == ""


def test_values_are_bounded_integers():
    assert valid_value(17) and valid_value(-20) and valid_value(60)
    for bad in (-21, 61, 1.5, "17", True, None):
        assert not valid_value(bad)


def test_cooldown_blocks_a_flood_but_not_other_players():
    now = [100.0]
    gate = Cooldown(2.0, clock=lambda: now[0])
    assert gate.ready("a")
    assert not gate.ready("a")
    assert gate.ready("b")
    now[0] += 2.5
    assert gate.ready("a")


@pytest.fixture
async def db(config: Config):
    config.ensure_dirs()
    database = Database(config.db_path, MIGRATIONS)
    await database.connect()
    yield database
    await database.close()


async def test_a_player_reporting_again_replaces_their_total(db):
    await db.add_initiative(10, "Thorin", 12)
    await db.add_initiative(10, "Thorin", 17)
    await db.add_initiative(10, "Wolf", 9)
    await db.add_initiative(11, "Elenya", 20)
    rows = await db.list_initiative()
    assert {(r["label"], r["value"]) for r in rows} == {("Thorin", 17), ("Wolf", 9), ("Elenya", 20)}


async def test_reports_are_bounded_and_expire(db, monkeypatch):
    monkeypatch.setattr("dnd_bot.db.MAX_INITIATIVE", 3)
    for n in range(6):
        await db.add_initiative(n, f"P{n}", n)
    assert len(await db.list_initiative()) == 3

    await db.conn.execute("UPDATE initiative_reports SET created_at = '2000-01-01T00:00:00+00:00'")
    await db.conn.commit()
    assert await db.list_initiative() == []


async def test_clear_one_or_all(db):
    await db.add_initiative(1, "A", 1)
    await db.add_initiative(2, "B", 2)
    first = (await db.list_initiative())[0]["id"]
    assert await db.clear_initiative(first) == 1
    assert [r["label"] for r in await db.list_initiative()] == ["B"]
    assert await db.clear_initiative() == 1


@pytest.fixture
async def client(config: Config, db: Database):
    bot = SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN),
        db=db,
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=None,
        is_ready=lambda: True,
    )
    async with TestClient(TestServer(build_app(bot))) as test_client:
        yield test_client


async def test_api_lists_labels_and_totals_but_no_user_ids(client, db):
    await db.add_initiative(123456789012345678, "Thorin", 17)
    response = await client.get("/api/v1/initiative", headers=AUTH)
    text = await response.text()
    body = json.loads(text)
    assert body[0]["label"] == "Thorin" and body[0]["value"] == 17
    assert set(body[0]) == {"id", "label", "value", "at"}
    assert "123456789012345678" not in text
    assert (await client.get("/api/v1/initiative")).status == 401


async def test_api_clear(client, db):
    await db.add_initiative(1, "A", 1)
    await db.add_initiative(2, "B", 2)
    first = (await (await client.get("/api/v1/initiative", headers=AUTH)).json())[0]["id"]
    one = await client.post("/api/v1/initiative/clear", json={"id": first}, headers=AUTH)
    assert (await one.json()) == {"removed": 1}
    everything = await client.post("/api/v1/initiative/clear", json={}, headers=AUTH)
    assert (await everything.json()) == {"removed": 1}
    for bad in ({"id": 0}, {"id": "1"}, {"id": True}, {"all": True}):
        assert (await client.post("/api/v1/initiative/clear", json=bad, headers=AUTH)).status == 400
