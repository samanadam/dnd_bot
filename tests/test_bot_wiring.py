"""Smoke test: the bot object builds and registers every documented command."""

from __future__ import annotations

from pathlib import Path

import pytest

discord = pytest.importorskip("discord", reason="py-cord is only installed with runtime deps")

from dnd_bot.bot import DnDBot, build_intents  # noqa: E402
from dnd_bot.db import Database  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

EXPECTED = {
    "session start",
    "session stop",
    "session status",
    "session cancel",
    "session list",
    "session transcript",
    "session recover",
    "session export",
    "character set",
    "character clear",
    "character list",
}


def test_intents_cover_voice_and_members():
    intents = build_intents()
    assert intents.guilds and intents.voice_states and intents.members


async def test_all_commands_are_registered(config, tmp_path: Path):
    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    try:
        bot = DnDBot(config, db)
        # Commands are only in `_application_commands` after a sync with Discord,
        # so inspect what is pending registration instead.
        names = set()
        for command in bot.pending_application_commands:
            if isinstance(command, discord.SlashCommandGroup):
                names.update(f"{command.name} {sub.name}" for sub in command.subcommands)
            else:
                names.add(command.name)
        assert EXPECTED <= names
    finally:
        await db.close()


async def test_second_channel_in_the_same_guild_is_rejected_clearly(config, tmp_path: Path):
    """Discord allows one voice connection per account per guild."""
    from types import SimpleNamespace

    from dnd_bot.recorder import ActiveSession, RecordingError
    from dnd_bot.timeutil import utcnow

    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    try:
        bot = DnDBot(config, db)
        existing = ActiveSession(
            session_id="s1",
            name="Main Table",
            guild_id=1,
            channel_id=2,
            channel_name="Table",
            text_channel_id=3,
            started_by_user_id=10,
            start_time=utcnow(),
            voice_client=SimpleNamespace(),
            sink=SimpleNamespace(),
        )
        bot.manager.active[(1, 2)] = existing

        other_channel = SimpleNamespace(id=99, name="Side Room", guild=SimpleNamespace(id=1))
        with pytest.raises(RecordingError) as excinfo:
            await bot.manager.start(
                channel=other_channel,
                text_channel_id=3,
                invoker=SimpleNamespace(id=11),
                name=None,
            )
        assert "Main Table" in str(excinfo.value)
        assert "one voice channel per server" in str(excinfo.value)
    finally:
        await db.close()


async def test_manager_starts_with_no_active_sessions(config, tmp_path: Path):
    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    try:
        bot = DnDBot(config, db)
        assert bot.manager.active == {}
        assert bot.manager.get(1, 2) is None
        assert bot.manager.sessions_in_guild(1) == []
    finally:
        await db.close()


# -- HTTP API lifecycle --------------------------------------------------------


async def test_no_api_server_when_it_is_disabled(config, tmp_path: Path):
    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    try:
        bot = DnDBot(config, db)
        assert bot.api is None
    finally:
        await db.close()


async def test_shutdown_stops_the_api_before_finalizing_sessions(config, tmp_path: Path):
    """Order matters: stop taking control requests, then flush audio to disk.

    The other way round, a request arriving mid-finalization races the very
    thing shutdown exists to protect.
    """
    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    order = []

    class SpyApi:
        async def stop(self):
            order.append("api")

    try:
        bot = DnDBot(config, db)
        bot.api = SpyApi()

        async def shutdown_all():
            order.append("sessions")
            return []

        bot.manager.shutdown_all = shutdown_all
        await bot.shutdown()
        assert order == ["api", "sessions"]
    finally:
        await db.close()


async def test_a_wedged_api_cannot_block_shutdown(config, tmp_path: Path):
    """Audio on disk beats a clean socket close, every time."""
    import asyncio

    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    finalized = []

    class WedgedApi:
        async def stop(self):
            await asyncio.sleep(3600)

    try:
        bot = DnDBot(config, db)
        bot.api = WedgedApi()
        bot._api_stop_timeout = 0.05

        async def shutdown_all():
            finalized.append(True)
            return []

        bot.manager.shutdown_all = shutdown_all
        await bot.shutdown()
        assert finalized == [True]
    finally:
        await db.close()


async def test_music_and_api_wire_together(config, tmp_path: Path):
    """The player must be reachable from the recorder, which drives its hooks."""
    from dataclasses import replace

    config = replace(
        config,
        api_enabled=True,
        api_token="t" * 32,
        music_enabled=True,
        music_youtube_enabled=True,
    )
    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    try:
        bot = DnDBot(config, db)
        assert bot.api is not None
        assert bot.music is not None
        assert bot.manager.music is bot.music
        # No R2 configured here, so YouTube is the only source on offer.
        assert set(bot.music.sources) == {"youtube"}
    finally:
        await db.close()
