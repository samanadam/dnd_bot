"""Renaming and deleting a session over HTTP, and that nothing else is touched."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import SESSION_START

from dnd_bot import paths, session_admin
from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.db import Database
from dnd_bot.timeutil import to_iso, utcnow

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


async def write_transcript(config: Config, session_id: str) -> None:
    path = paths.transcript_json_path(config.sessions_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    segment = {
        "speaker": "Old",
        "user_id": "10",
        "start": 0.0,
        "end": 1.0,
        "text": "the dragon sleeps",
    }
    path.write_text(json.dumps({"segments": [segment]}), encoding="utf-8")


async def post(client, session_id, action, body):
    return await client.post(f"/api/v1/sessions/{session_id}/{action}", json=body, headers=AUTH)


# -- auth ---------------------------------------------------------------------


@pytest.mark.parametrize("action", ["update", "trash", "restore", "purge"])
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


# -- trash --------------------------------------------------------------------


async def trash(client, session_id="s1"):
    return await post(client, session_id, "trash", {"confirm_id": session_id})


async def test_trashing_hides_a_session_everywhere_but_keeps_its_files(client, db, config):
    await seed(db)
    await db.mark_session_state("s1", "waiting")
    made = leave_files(config, "s1")

    response = await trash(client)
    assert response.status == 200
    body = await response.json()
    assert body["id"] == "s1"
    assert body["deleted_at"] and body["purge_at"]

    assert all(path.exists() for path in made)
    assert await db.get_session("s1") is not None
    assert client.bot.store.deleted == []
    assert await (await client.get("/api/v1/sessions", headers=AUTH)).json() == []
    assert (await client.get("/api/v1/sessions/s1/transcript", headers=AUTH)).status == 404
    queue = await (await client.get("/api/v1/transcription", headers=AUTH)).json()
    assert queue["items"] == []
    assert await db.pending_count() == 0
    listed = await (await client.get("/api/v1/sessions/trash", headers=AUTH)).json()
    assert [row["id"] for row in listed] == ["s1"]


async def test_a_trashed_session_cannot_be_changed(client, db):
    await seed(db)
    await trash(client)
    assert (await post(client, "s1", "update", {"name": "x"})).status == 404
    assert (await post(client, "s1", "campaign", {"campaign_id": None})).status == 404
    assert (await trash(client)).status == 409


async def test_a_trashed_session_stops_counting_toward_its_campaign(client, db):
    campaign = await db.create_campaign(name="Strahd", channel_id=None, language=None)
    await seed(db)
    await db.assign_session_campaign("s1", campaign["id"])
    assert (await db.list_campaigns())[0]["session_count"] == 1
    await trash(client)
    assert (await db.list_campaigns())[0]["session_count"] == 0
    await post(client, "s1", "restore", {"confirm_id": "s1"})
    assert (await db.list_campaigns())[0]["session_count"] == 1


async def test_trashed_transcripts_leave_search_and_come_back_on_restore(client, db, config):
    await seed(db)
    await write_transcript(config, "s1")
    await db.update_session("s1", transcribed=1)

    async def hits():
        body = await (await client.get("/api/v1/transcripts/search?q=dragon", headers=AUTH)).json()
        return [hit["session_id"] for hit in body["results"]]

    assert await hits() == ["s1"]
    await trash(client)
    assert await hits() == []
    assert (await post(client, "s1", "restore", {"confirm_id": "s1"})).status == 200
    assert await hits() == ["s1"]


@pytest.mark.parametrize(
    "body", [{}, {"confirm_id": "other"}, {"confirm_id": True}, {"confirm": "s1"}]
)
@pytest.mark.parametrize("action", ["trash", "restore", "purge"])
async def test_every_change_needs_the_session_named_in_the_body(client, db, config, action, body):
    await seed(db)
    if action != "trash":
        await session_admin.trash(db, "s1")
    made = leave_files(config, "s1")
    assert (await post(client, "s1", action, body)).status == 400
    assert all(path.exists() for path in made)
    assert client.bot.store.deleted == []
    assert bool((await db.get_session("s1"))["deleted_at"]) == (action != "trash")


async def test_unknown_session(client):
    for action in ("trash", "restore", "purge"):
        assert (await post(client, "nope", action, {"confirm_id": "nope"})).status == 404


async def test_a_recording_cannot_be_trashed(client, db):
    await seed(db, completed=False)
    client.bot.manager.active[(1, 2)] = SimpleNamespace(session_id="s1")
    assert (await trash(client)).status == 409
    assert (await db.get_session("s1"))["deleted_at"] is None


# -- restore ------------------------------------------------------------------


async def test_restore_puts_the_session_back(client, db):
    await seed(db)
    await trash(client)
    response = await post(client, "s1", "restore", {"confirm_id": "s1"})
    assert response.status == 200
    assert (await response.json())["id"] == "s1"
    listed = await (await client.get("/api/v1/sessions", headers=AUTH)).json()
    assert [row["id"] for row in listed] == ["s1"]
    assert await (await client.get("/api/v1/sessions/trash", headers=AUTH)).json() == []


async def test_restore_of_a_session_not_in_the_trash_is_refused(client, db):
    await seed(db)
    assert (await post(client, "s1", "restore", {"confirm_id": "s1"})).status == 409


# -- purge --------------------------------------------------------------------


async def test_purge_needs_the_trash_first(client, db, config):
    await seed(db)
    made = leave_files(config, "s1")
    assert (await post(client, "s1", "purge", {"confirm_id": "s1"})).status == 409
    assert all(path.exists() for path in made)
    assert await db.get_session("s1") is not None


async def test_purge_removes_files_rows_index_and_remote_copies(client, db, config):
    await seed(db)
    await db.mark_session_state("s1", "waiting")
    made = leave_files(config, "s1")
    await trash(client)

    response = await post(client, "s1", "purge", {"confirm_id": "s1"})
    assert response.status == 200
    body = await response.json()
    assert body["purged"] == "s1"
    assert body["files_removed"] == 5
    assert body["bytes_freed"] == 50
    assert body["remote_objects_removed"] == 4

    assert not any(path.exists() for path in made)
    assert await db.get_session("s1") is None
    assert await db.session_state("s1") is None
    assert client.bot.store.deleted == [("outbox", "s1"), ("inbox", "s1")]


async def test_purge_leaves_other_sessions_alone(client, db, config):
    await seed(db, "s1")
    await seed(db, "s2")
    keep = leave_files(config, "s2")
    leave_files(config, "s1")
    await trash(client, "s1")

    assert (await post(client, "s1", "purge", {"confirm_id": "s1"})).status == 200
    assert all(path.exists() for path in keep)
    assert await db.get_session("s2") is not None


async def test_a_path_like_id_never_reaches_the_disk(client, config):
    outside = config.data_dir / "keep.txt"
    outside.write_text("keep")
    for action in ("trash", "purge"):
        response = await client.post(
            f"/api/v1/sessions/..%2f..%2fkeep/{action}", json={}, headers=AUTH
        )
        assert response.status in (400, 404)
    assert outside.exists()


async def test_purge_refused_while_the_transcriber_has_it(client, db, config):
    await seed(db)
    await db.mark_session_state("s1", "transcribing")
    made = leave_files(config, "s1")
    await trash(client)
    assert (await post(client, "s1", "purge", {"confirm_id": "s1"})).status == 409
    assert await db.get_session("s1") is not None
    assert all(path.exists() for path in made)


async def test_purge_works_with_nothing_on_disk_and_no_bucket(client, db):
    await seed(db)
    client.bot.store = None
    await trash(client)
    response = await post(client, "s1", "purge", {"confirm_id": "s1"})
    assert response.status == 200
    assert (await response.json())["files_removed"] == 0


async def test_a_bucket_failure_keeps_the_session_so_it_can_be_retried(client, db, config):
    await seed(db)
    leave_files(config, "s1")
    await trash(client)
    client.bot.store = FakeStore(fail=True)

    response = await post(client, "s1", "purge", {"confirm_id": "s1"})
    assert response.status == 502
    assert "bucket unreachable" not in await response.text()
    assert (await db.get_session("s1"))["deleted_at"] is not None

    client.bot.store = FakeStore()
    assert (await post(client, "s1", "purge", {"confirm_id": "s1"})).status == 200
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
    await trash(client)

    assert (await post(client, "s1", "purge", {"confirm_id": "s1"})).status == 409
    assert (target / "file").exists()
    assert await db.get_session("s1") is not None


async def test_purged_transcripts_leave_the_search_index(client, db, config):
    await seed(db)
    await write_transcript(config, "s1")
    await db.update_session("s1", transcribed=1)
    await client.get("/api/v1/transcripts/search?q=dragon", headers=AUTH)
    indexed = await db.conn.execute_fetchall("SELECT count(*) FROM transcript_fts")
    assert indexed[0][0] == 1

    await trash(client)
    assert (await post(client, "s1", "purge", {"confirm_id": "s1"})).status == 200
    rows = await db.conn.execute_fetchall("SELECT count(*) FROM transcript_fts")
    assert rows[0][0] == 0


def test_purge_has_the_tight_rate_limit():
    from dnd_bot.api.auth import limit_for

    assert limit_for("/api/v1/sessions/s1/purge", 120) == ("tight", 10)
    assert limit_for("/api/v1/sessions/s1/trash", 120) == ("general", 120)


# -- emptying the trash on a schedule ------------------------------------------


async def test_only_sessions_past_the_window_are_emptied(db, config):
    for name in ("old", "recent", "busy", "kept"):
        await seed(db, name)
    now = utcnow()
    await db.update_session("old", deleted_at=to_iso(now - timedelta(days=8)))
    await db.update_session("recent", deleted_at=to_iso(now - timedelta(days=2)))
    await db.update_session("busy", deleted_at=to_iso(now - timedelta(days=30)))
    await db.mark_session_state("busy", "transcribing")
    old_files = leave_files(config, "old")
    recent_files = leave_files(config, "recent")

    purged = await session_admin.purge_expired(db, config, None, None, now=now)

    assert purged == ["old"]
    assert not any(path.exists() for path in old_files)
    assert all(path.exists() for path in recent_files)
    assert await db.get_session("old") is None
    for kept in ("recent", "busy", "kept"):
        assert await db.get_session(kept) is not None


async def test_a_stuck_session_does_not_stop_the_rest(db, config):
    await seed(db, "a")
    await seed(db, "b")
    long_ago = to_iso(utcnow() - timedelta(days=30))
    await db.update_session("a", deleted_at=long_ago)
    await db.update_session("b", deleted_at=long_ago)
    target = config.data_dir / "elsewhere"
    target.mkdir()
    try:
        paths.session_dir(config.sessions_dir, "a").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges on this platform")

    assert await session_admin.purge_expired(db, config, None, None) == ["b"]
    assert await db.get_session("a") is not None
