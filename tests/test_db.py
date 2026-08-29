"""Schema/migration idempotency and the queue's single-open-job invariant."""

from __future__ import annotations

from pathlib import Path

import pytest

from dnd_bot.db import Database, discover_migrations
from dnd_bot.timeutil import to_iso, utcnow

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


async def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    return db


async def seed_session(db: Database, session_id: str = "s1") -> None:
    await db.create_session(
        session_id=session_id,
        name="Test",
        guild_id=1,
        channel_id=2,
        channel_name="Table",
        text_channel_id=3,
        started_by_user_id=10,
        start_time=to_iso(utcnow()),
        participants={"10": "Thorin"},
        language="tr",
        model_used="medium",
    )


def test_migrations_are_discovered_in_numeric_order():
    versions = [m.version for m in discover_migrations(MIGRATIONS)]
    assert versions == sorted(versions)
    assert 1 in versions


async def test_applying_the_schema_twice_is_a_noop(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        assert await db.migrate() == []  # already applied by connect()
        await seed_session(db)
        assert await db.migrate() == []
        assert (await db.get_session("s1")) is not None
    finally:
        await db.close()


async def test_reconnecting_reapplies_nothing_and_keeps_data(tmp_path: Path):
    db = await make_db(tmp_path)
    await seed_session(db)
    await db.close()

    db2 = await make_db(tmp_path)
    try:
        rows = await db2.list_open_sessions()
        assert [r["id"] for r in rows] == ["s1"]
    finally:
        await db2.close()


async def test_wal_mode_is_enabled(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        cursor = await db.conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0].lower() == "wal"
    finally:
        await db.close()


async def test_a_session_is_only_exported_once(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        await seed_session(db)
        assert await db.mark_exported("s1") is not None
        assert await db.mark_exported("s1") is None
        assert await db.pending_count() == 1
        assert await db.session_state("s1") == "exported"
    finally:
        await db.close()


async def test_a_session_moves_through_the_handover(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        await seed_session(db)
        await db.mark_exported("s1")

        waiting = await db.awaiting_transcription()
        assert [row["session_id"] for row in waiting] == ["s1"]

        await db.mark_session_state("s1", "done")
        assert await db.session_state("s1") == "done"
        assert await db.pending_count() == 0
        assert await db.awaiting_transcription() == []
    finally:
        await db.close()


async def test_state_can_be_set_for_a_session_never_exported(tmp_path: Path):
    """A recovered session may reach the handover table late."""
    db = await make_db(tmp_path)
    try:
        await seed_session(db)
        await db.mark_session_state("s1", "done")
        assert await db.session_state("s1") == "done"
    finally:
        await db.close()


async def test_character_mapping_upserts(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        await db.set_character(10, "Thorin")
        await db.set_character(10, "Thorin II")
        assert await db.character_map() == {"10": "Thorin II"}
        await db.clear_character(10)
        assert await db.character_map() == {}
    finally:
        await db.close()


async def test_active_session_lookup_is_per_channel(tmp_path: Path):
    db = await make_db(tmp_path)
    try:
        await seed_session(db, "main")
        await db.create_session(
            session_id="side",
            name="Side",
            guild_id=1,
            channel_id=99,
            channel_name="Side",
            text_channel_id=3,
            started_by_user_id=11,
            start_time=to_iso(utcnow()),
            participants={},
            language="tr",
            model_used="medium",
        )
        assert (await db.active_session_in_channel(1, 2))["id"] == "main"
        assert (await db.active_session_in_channel(1, 99))["id"] == "side"
        assert await db.active_session_in_channel(1, 1234) is None
    finally:
        await db.close()


async def test_unconnected_database_fails_loudly(tmp_path: Path):
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    with pytest.raises(RuntimeError):
        _ = db.conn
