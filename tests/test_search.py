"""Transcript search and the transcription queue."""

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
from dnd_bot.search import TranscriptIndex, match_expression
from dnd_bot.timeutil import to_iso
from dnd_bot.transcripts import TranscriptReader

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def test_match_expression_neutralises_fts_syntax():
    assert match_expression("el drin") == '"el" "drin"*'
    assert (
        match_expression('Eldrin OR "x" NEAR(a b) -c *') == '"Eldrin" "OR" "x" "NEAR" "a" "b" "c"*'
    )
    for nothing in ("", "   ", '"*-', "()"):
        with pytest.raises(ValueError):
            match_expression(nothing)
    assert len(match_expression("a b c d e f g h i j k l").split()) == 8


@pytest.fixture
async def db(config: Config):
    config.ensure_dirs()
    database = Database(config.db_path, MIGRATIONS)
    await database.connect()
    yield database
    await database.close()


async def add_session(db, config, session_id, segments, *, campaign_id=None, transcribed=True):
    await db.create_session(
        session_id=session_id,
        name=f"Game {session_id}",
        guild_id=1,
        channel_id=2,
        channel_name="Table",
        text_channel_id=None,
        started_by_user_id=10,
        start_time=to_iso(SESSION_START),
        participants={"10": "Old"},
        language="tr",
        campaign_id=campaign_id,
        base_labels={"10": "Aylin"},
    )
    await db.update_session(
        session_id,
        completed=1,
        transcribed=int(transcribed),
        end_time=to_iso(SESSION_START.replace(hour=20)),
    )
    path = paths.transcript_json_path(config.sessions_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [
        {"speaker": "Old", "user_id": "10", "start": float(i), "end": i + 1.0, "text": text}
        for i, text in enumerate(segments)
    ]
    path.write_text(json.dumps({"segments": body}), encoding="utf-8")


async def test_search_finds_passages_across_sessions_with_context(db, config):
    await add_session(db, config, "s1", ["Kapıda bir bekçi vardı", "Eldrin geldi"])
    await add_session(db, config, "s2", ["Eldrin kılıcını çekti"])
    index = TranscriptIndex(db, TranscriptReader(config.sessions_dir))

    hits, pending = await index.search("eldr")

    assert pending == 0
    assert {(h.session_id, h.seq) for h in hits} == {("s1", 1), ("s2", 0)}
    assert all("[[Eldrin]]" in h.snippet for h in hits)
    assert hits[0].speaker == "Old" and hits[0].start is not None


async def test_search_ignores_diacritics_and_operators(db, config):
    await add_session(db, config, "s1", ["Şehirde çok güzel bir gün"])
    index = TranscriptIndex(db, TranscriptReader(config.sessions_dir))
    assert [h.seq for h in (await index.search("sehirde"))[0]] == [0]
    # Operators typed into the box are ordinary words, not FTS syntax: no error,
    # and "OR" has to be in the text to match.
    assert (await index.search('güzel" OR *'))[0] == []
    assert [h.seq for h in (await index.search("güzel*"))[0]] == [0]
    with pytest.raises(ValueError):
        await index.search('"" *')


async def test_search_skips_sessions_without_a_transcript(db, config):
    await add_session(db, config, "s1", ["gizli sözcük"], transcribed=False)
    index = TranscriptIndex(db, TranscriptReader(config.sessions_dir))
    assert (await index.search("gizli"))[0] == []


async def test_search_reads_the_way_the_transcript_is_shown(db, config):
    campaign = await db.create_campaign(name="Strahd")
    await db.replace_corrections(campaign["id"], [("el drin", "Eldrin")])
    await db.set_campaign_character(campaign["id"], 10, "Thorin")
    await add_session(db, config, "s1", ["el drin geldi"])
    index = TranscriptIndex(db, TranscriptReader(config.sessions_dir))

    assert (await index.search("eldrin"))[0] == []

    await db.assign_session_campaign("s1", campaign["id"])
    hits, _ = await index.search("eldrin")
    assert [(h.seq, h.speaker, h.campaign_name) for h in hits] == [(0, "Thorin", "Strahd")]

    await db.assign_session_campaign("s1", None)
    assert (await index.search("eldrin"))[0] == []


async def test_search_can_be_limited_to_one_campaign_or_unassigned(db, config):
    campaign = await db.create_campaign(name="Strahd")
    await add_session(db, config, "in", ["ejderha uçtu"], campaign_id=campaign["id"])
    await add_session(db, config, "out", ["ejderha kaçtı"])
    index = TranscriptIndex(db, TranscriptReader(config.sessions_dir))

    ids = lambda hits: {h.session_id for h in hits[0]}  # noqa: E731
    assert ids(await index.search("ejderha")) == {"in", "out"}
    assert ids(await index.search("ejderha", campaign=campaign["id"])) == {"in"}
    assert ids(await index.search("ejderha", campaign="unassigned")) == {"out"}


async def test_a_long_history_is_indexed_in_batches(db, config, monkeypatch):
    monkeypatch.setattr("dnd_bot.search.INDEX_BATCH", 2)
    for n in range(5):
        await add_session(db, config, f"s{n}", ["ortak kelime"])
    index = TranscriptIndex(db, TranscriptReader(config.sessions_dir))

    first, pending = await index.search("ortak")
    assert len(first) == 2 and pending == 3
    second, pending = await index.search("ortak")
    assert len(second) == 4 and pending == 1
    third, pending = await index.search("ortak")
    assert len(third) == 5 and pending == 0


# -- HTTP ---------------------------------------------------------------------


class FakeUploader:
    def __init__(self):
        self.runs = 0

    async def run_once(self):
        self.runs += 1
        return ["x"]


class FakeFetcher(FakeUploader):
    pass


@pytest.fixture
async def client(config: Config, db: Database):
    bot = SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN),
        db=db,
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=None,
        is_ready=lambda: True,
        uploader=FakeUploader(),
        fetcher=FakeFetcher(),
    )
    async with TestClient(TestServer(build_app(bot))) as test_client:
        test_client.bot = bot
        yield test_client


async def test_search_route(client, db, config):
    await add_session(db, config, "s1", ["Eldrin geldi"])
    response = await client.get("/api/v1/transcripts/search?q=eldrin", headers=AUTH)
    assert response.status == 200
    body = await response.json()
    assert body["query"] == "eldrin" and body["still_indexing"] == 0
    assert body["results"][0]["session_id"] == "s1"
    assert "user_id" not in json.dumps(body)


@pytest.mark.parametrize(
    "query", ["", "q=", "q=" + "x" * 201, "q=a&limit=0", "q=a&limit=101", "q=a&campaign=zz;--"]
)
async def test_search_route_validates(client, query):
    path = "/api/v1/transcripts/search" + (f"?{query}" if query else "")
    assert (await client.get(path, headers=AUTH)).status == 400


async def test_queue_reports_where_the_audio_is(client, db, config):
    await add_session(db, config, "up", ["x"], transcribed=False)
    await add_session(db, config, "cloud", ["x"], transcribed=False)
    await add_session(db, config, "old", ["x"], transcribed=False)
    for session_id in ("up", "cloud", "old"):
        await db.mark_exported(session_id)
    staged = config.outbox_dir / "up"
    staged.mkdir(parents=True)
    (staged / "READY").write_text("")
    await db.conn.execute(
        "UPDATE transcription_queue SET queued_at = ? WHERE session_id = 'old'",
        ("2000-01-01T00:00:00+00:00",),
    )
    await db.conn.commit()

    body = await (await client.get("/api/v1/transcription", headers=AUTH)).json()

    by_id = {item["session_id"]: item for item in body["items"]}
    assert by_id["up"]["status"] == "uploading"
    assert by_id["cloud"]["status"] == "waiting" and by_id["cloud"]["stalled"] is False
    assert by_id["old"]["stalled"] is True
    assert body["can_sync"] is True


async def test_sync_runs_both_passes_once(client):
    response = await client.post("/api/v1/transcription/sync", headers=AUTH)
    assert response.status == 200
    body = await response.json()
    assert (body["uploaded"], body["fetched"]) == (1, 1)
    assert client.bot.uploader.runs == 1 and client.bot.fetcher.runs == 1


async def test_sync_without_r2_has_nothing_to_do(client):
    client.bot.uploader = None
    client.bot.fetcher = None
    assert (await client.post("/api/v1/transcription/sync", headers=AUTH)).status == 409
