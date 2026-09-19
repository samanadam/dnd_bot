"""Renaming and deleting a session over HTTP, and that nothing else is touched."""

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


class FakeStore:
    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.fail = fail

    def delete_session(self, prefix: str, session_id: str) -> int:
        if self.fail:
            raise RuntimeError("bucket unreachable")
        self.deleted.append((prefix, session_id))
        return 2


@pytest.fixture
async def db(config: Config):
    config.ensure_dirs()
    database = Database(config.db_path, MIGRATIONS)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def client(config: Config, db: Database):
    bot = SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN),
        db=db,
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=FakeStore(),
        music=None,
        is_ready=lambda: True,
        get_guild=lambda gid: None,
    )
    async with TestClient(TestServer(build_app(bot))) as test_client:
        test_client.bot = bot
        yield test_client


async def seed(db: Database, session_id="s1", completed=True) -> None:
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


def leave_files(config: Config, session_id: str) -> list[Path]:
    """Everything the bot keeps on disk for one session."""
    made = [
        paths.audio_dir(config.sessions_dir, session_id) / "10.wav",
        paths.transcript_md_path(config.sessions_dir, session_id),
        config.inbox_dir / session_id / "transcript.md",
        config.outbox_dir / session_id / "10.opus",
        paths.export_path(config.exports_dir, session_id),
    ]
    for path in made:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    return made


async def post(client, session_id, action, body):
    return await client.post(f"/api/v1/sessions/{session_id}/{action}", json=body, headers=AUTH)


# -- auth ---------------------------------------------------------------------


@pytest.mark.parametrize("action", ["update", "delete"])
async def test_needs_the_token(client, action):
    response = await client.post(f"/api/v1/sessions/s1/{action}", json={})
    assert response.status == 401


# -- rename -------------------------------------------------------------------


async def test_rename_changes_only_the_name(client, db):
    await seed(db)
    response = await post(client, "s1", "update", {"name": "  Session zero  "})
    assert response.status == 200
    body = await response.json()
    assert body["name"] == "Session zero"
    assert body["id"] == "s1"
    assert (await db.get_session("s1"))["channel_name"] == "Table"


@pytest.mark.parametrize(
    "body",
    [{}, {"name": ""}, {"name": "   "}, {"name": "x" * 101}, {"name": 5}, {"name": "a\nb"}]
    + [{"name": "ok", "completed": 0}, {"name": "ok", "id": "other"}],
)
async def test_rename_validates_the_body(client, db, body):
    await seed(db)
    assert (await post(client, "s1", "update", body)).status == 400
    assert (await db.get_session("s1"))["name"] == "Test"


async def test_rename_unknown_session(client):
    assert (await post(client, "nope", "update", {"name": "x"})).status == 404


async def test_rename_refused_while_recording(client, db):
    await seed(db, completed=False)
    client.bot.manager.active[(1, 2)] = SimpleNamespace(session_id="s1")
    assert (await post(client, "s1", "update", {"name": "x"})).status == 409


# -- delete -------------------------------------------------------------------


async def test_delete_removes_files_rows_index_and_remote_copies(client, db, config):
    await seed(db)
    await db.mark_session_state("s1", "waiting")
    made = leave_files(config, "s1")

    response = await post(client, "s1", "delete", {"confirm_id": "s1"})
    assert response.status == 200
    body = await response.json()
    assert body["deleted"] == "s1"
    assert body["files_removed"] == 5
    assert body["bytes_freed"] == 50
    assert body["remote_objects_removed"] == 4

    assert not any(path.exists() for path in made)
    assert await db.get_session("s1") is None
    assert await db.session_state("s1") is None
    assert client.bot.store.deleted == [("outbox", "s1"), ("inbox", "s1")]


async def test_delete_leaves_other_sessions_alone(client, db, config):
    await seed(db, "s1")
    await seed(db, "s2")
    keep = leave_files(config, "s2")
    leave_files(config, "s1")

    assert (await post(client, "s1", "delete", {"confirm_id": "s1"})).status == 200
    assert all(path.exists() for path in keep)
    assert await db.get_session("s2") is not None


@pytest.mark.parametrize(
    "body", [{}, {"confirm_id": "other"}, {"confirm_id": True}, {"confirm": "s1"}]
)
async def test_delete_needs_the_session_named_in_the_body(client, db, config, body):
    await seed(db)
    made = leave_files(config, "s1")
    assert (await post(client, "s1", "delete", body)).status == 400
    assert await db.get_session("s1") is not None
    assert all(path.exists() for path in made)
    assert client.bot.store.deleted == []


async def test_delete_unknown_session(client):
    assert (await post(client, "nope", "delete", {"confirm_id": "nope"})).status == 404


async def test_a_path_like_id_never_reaches_the_disk(client, config):
    outside = config.data_dir / "keep.txt"
    outside.write_text("keep")
    response = await client.post("/api/v1/sessions/..%2f..%2fkeep/delete", json={}, headers=AUTH)
    assert response.status in (400, 404)
    assert outside.exists()


async def test_delete_refused_while_recording(client, db, config):
    await seed(db, completed=False)
    made = leave_files(config, "s1")
    client.bot.manager.active[(1, 2)] = SimpleNamespace(session_id="s1")
    assert (await post(client, "s1", "delete", {"confirm_id": "s1"})).status == 409
    assert all(path.exists() for path in made)


async def test_delete_refused_while_the_transcriber_has_it(client, db, config):
    await seed(db)
    await db.mark_session_state("s1", "transcribing")
    made = leave_files(config, "s1")
    assert (await post(client, "s1", "delete", {"confirm_id": "s1"})).status == 409
    assert await db.get_session("s1") is not None
    assert all(path.exists() for path in made)


async def test_an_interrupted_session_can_be_deleted(client, db, config):
    await seed(db, completed=False)
    leave_files(config, "s1")
    assert (await post(client, "s1", "delete", {"confirm_id": "s1"})).status == 200
    assert await db.get_session("s1") is None


async def test_delete_works_with_nothing_on_disk_and_no_bucket(client, db):
    await seed(db)
    client.bot.store = None
    response = await post(client, "s1", "delete", {"confirm_id": "s1"})
    assert response.status == 200
    assert (await response.json())["files_removed"] == 0


async def test_a_bucket_failure_keeps_the_session_so_it_can_be_retried(client, db, config):
    await seed(db)
    leave_files(config, "s1")
    client.bot.store = FakeStore(fail=True)

    response = await post(client, "s1", "delete", {"confirm_id": "s1"})
    assert response.status == 502
    text = await response.text()
    assert "bucket unreachable" not in text
    assert await db.get_session("s1") is not None

    client.bot.store = FakeStore()
    assert (await post(client, "s1", "delete", {"confirm_id": "s1"})).status == 200
    assert await db.get_session("s1") is None


async def test_a_symlinked_session_folder_is_refused_and_not_followed(client, db, config):
    await seed(db)
    target = config.data_dir / "precious"
    target.mkdir()
    (target / "file").write_text("keep")
    link = paths.session_dir(config.sessions_dir, "s1")
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges on this platform")

    assert (await post(client, "s1", "delete", {"confirm_id": "s1"})).status == 409
    assert (target / "file").exists()
    assert await db.get_session("s1") is not None


async def test_deleted_transcripts_leave_the_search_index(client, db, config):
    await seed(db)
    path = paths.transcript_json_path(config.sessions_dir, "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    segment = {
        "speaker": "Old",
        "user_id": "10",
        "start": 0.0,
        "end": 1.0,
        "text": "the dragon sleeps",
    }
    path.write_text(json.dumps({"segments": [segment]}), encoding="utf-8")
    await db.update_session("s1", transcribed=1)

    found = await (await client.get("/api/v1/transcripts/search?q=dragon", headers=AUTH)).json()
    assert [hit["session_id"] for hit in found["results"]] == ["s1"]

    assert (await post(client, "s1", "delete", {"confirm_id": "s1"})).status == 200
    after = await (await client.get("/api/v1/transcripts/search?q=dragon", headers=AUTH)).json()
    assert after["results"] == []
    rows = await db.conn.execute_fetchall(
        "SELECT count(*) FROM transcript_fts WHERE session_id = 's1'"
    )
    assert rows[0][0] == 0


def test_delete_has_the_tight_rate_limit():
    from dnd_bot.api.auth import limit_for

    assert limit_for("/api/v1/sessions/s1/delete", 120) == ("tight", 10)
    assert limit_for("/api/v1/sessions/s1/update", 120) == ("general", 120)
