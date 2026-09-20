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
        assert set(bot.music.sources) == {"youtube", "soundcloud"}
    finally:
        await db.close()


# -- storage reachability reported to the dashboard ---------------------------
#
# The API used to report `storage.reachable` as a constant None: it was set once
# when the app was built and nothing ever updated it. A dashboard that reads
# null as falsy then shows storage as "Down" permanently, while R2 works.


class ReachableStore:
    def list_keys(self, prefix):
        return []


class UnreachableStore:
    def list_keys(self, prefix):
        raise ConnectionError("no route to host")


async def _bot_with_store(config, tmp_path, store):
    from dataclasses import replace

    config = replace(config, storage_backend="r2", r2_bucket="bucket")
    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    bot = DnDBot.__new__(DnDBot)
    bot.config = config
    bot.db = db
    bot.store = store
    bot.storage_reachable = None

    class Notifier:
        async def send_dm(self, *args, **kwargs):
            return None

    bot.notifier = Notifier()
    return bot, db


async def test_a_reachable_bucket_is_reported_reachable(config, tmp_path: Path):
    bot, db = await _bot_with_store(config, tmp_path, ReachableStore())
    try:
        await bot._check_object_storage()
        assert bot.storage_reachable is True
    finally:
        await db.close()


async def test_an_unreachable_bucket_is_reported_down(config, tmp_path: Path):
    bot, db = await _bot_with_store(config, tmp_path, UnreachableStore())
    try:
        await bot._check_object_storage()
        assert bot.storage_reachable is False
    finally:
        await db.close()


async def test_local_storage_has_nothing_to_reach(config, tmp_path: Path):
    """No bucket means "not applicable", which must not read as "down"."""
    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    try:
        bot = DnDBot(config, db)
        await bot._check_object_storage()
        assert bot.storage_reachable is None
    finally:
        await db.close()


async def test_stats_reports_the_live_reachability(config, tmp_path: Path):
    """The value the dashboard sees must be the bot's, not a startup constant."""
    from dataclasses import replace
    from types import SimpleNamespace

    from aiohttp.test_utils import TestClient, TestServer

    from dnd_bot.api.server import build_app

    api_config = replace(config, api_enabled=True, api_token="t" * 32)
    api_config.ensure_dirs()

    async def zero():
        return 0

    bot = SimpleNamespace(
        config=api_config,
        db=SimpleNamespace(pending_count=zero),
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda g: []),
        store=None,
        music=None,
        is_ready=lambda: True,
        storage_reachable=True,
    )
    headers = {"Authorization": "Bearer " + "t" * 32}
    async with TestClient(TestServer(build_app(bot))) as client:
        first = await (await client.get("/api/v1/stats", headers=headers)).json()
        bot.storage_reachable = False
        second = await (await client.get("/api/v1/stats", headers=headers)).json()

    assert first["storage"]["reachable"] is True
    assert second["storage"]["reachable"] is False
