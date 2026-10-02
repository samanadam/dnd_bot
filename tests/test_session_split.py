"""Splitting a long game into parts without anyone leaving the channel.

Driven against the same fake Discord the lifecycle tests use. The point of a
split is that nothing is lost and nothing waits: the next part is recording the
moment the call returns, on the same connection, while the part just closed is
still being encoded in the background.
"""

from __future__ import annotations

import asyncio
import threading
import wave
from dataclasses import replace

import pytest
from test_session_lifecycle import (  # noqa: F401 - FakeReader is used via FakeChannel
    ELENYA,
    THORIN,
    FakeChannel,
    FakeVoiceClient,
)

from dnd_bot import recorder
from dnd_bot.contract import READY_MARKER, is_marked
from dnd_bot.db import Database
from dnd_bot.recorder import RecordingError, SessionManager


class FakeTextChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str) -> None:
        self.sent.append(content)


class FakeBot:
    """Just enough of discord.Bot for the recorder's background work."""

    user = None

    def __init__(self) -> None:
        self.text = FakeTextChannel()
        self.voice: FakeChannel | None = None

    def get_channel(self, channel_id: int):
        if channel_id == 3:
            return self.text
        if channel_id == 2:
            return self.voice
        return None

    def get_guild(self, guild_id: int):
        return None


@pytest.fixture
async def rig(config):
    """(manager, db, config, bot) with WAV output, so no ffmpeg is needed."""
    config = replace(config, audio_format="wav", break_reminder_hours=0.0)
    config.ensure_dirs()
    db = Database(config.db_path)
    await db.connect()
    bot = FakeBot()
    yield SessionManager(bot=bot, db=db, config=config), db, config, bot
    await db.close()


def wav_seconds(path) -> float:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() / reader.getframerate()


async def start_game(rig, name="Kamp", members=(THORIN, ELENYA), campaign_id=None):
    mgr, _db, _config, bot = rig
    channel = FakeChannel(list(members))
    bot.voice = channel
    session = await mgr.start(
        channel=channel,
        text_channel_id=3,
        invoker=THORIN,
        name=name,
        campaign_id=campaign_id,
    )
    return channel, session


# -- the split itself --------------------------------------------------------------


async def test_a_split_hands_the_game_on_without_a_gap(rig):
    mgr, db, config, _bot = rig
    channel, first = await start_game(rig)
    channel.voice_client.speak(THORIN, 0.5)

    result = await mgr.split(channel=channel)
    channel.voice_client.speak(THORIN, 0.25)
    channel.voice_client.speak(ELENYA, 0.25)

    assert result.session.name == "Kamp (part 2)"
    assert result.session.part == 2
    assert result.previous_id == first.session_id
    assert result.previous_name == "Kamp (part 1)"
    assert mgr.get(1, 2) is result.session
    # Same connection: nobody was disconnected and the recorder was swapped in place.
    assert channel.voice_client.recording
    assert not channel.voice_client.disconnected
    assert channel.voice_client.stops == 0
    assert channel.voice_client.sink is result.session.sink

    previous = await result.finishing
    assert previous is not None and previous.enqueued, previous
    assert previous.speakers == ["Thorin"]
    staged_first = config.outbox_dir / first.session_id
    assert is_marked(staged_first, READY_MARKER)
    [first_track] = staged_first.glob("*.wav")
    assert wav_seconds(first_track) == pytest.approx(0.5, abs=0.01)

    second = await mgr.stop(1, 2)
    assert second.enqueued
    assert sorted(second.speakers) == ["Thorin", "aylin"]
    staged_second = config.outbox_dir / result.session.session_id
    for track in staged_second.glob("*.wav"):
        # Only what was said after the split, not the half second before it.
        assert wav_seconds(track) == pytest.approx(0.25, abs=0.01)

    assert (await db.get_session(first.session_id))["name"] == "Kamp (part 1)"
    assert (await db.get_session(first.session_id))["completed"] == 1
    assert (await db.get_session(result.session.session_id))["completed"] == 1


async def test_the_next_part_keeps_the_campaign_channel_and_starter(rig):
    mgr, db, _config, _bot = rig
    campaign = await db.create_campaign(name="Saga", language="en")
    channel, first = await start_game(rig, campaign_id=campaign["id"])

    result = await mgr.split(channel=channel)

    row = await db.get_session(result.session.session_id)
    assert row["campaign_id"] == campaign["id"]
    assert row["language"] == (await db.get_session(first.session_id))["language"]
    assert row["text_channel_id"] == "3"
    assert row["started_by_user_id"] == str(THORIN.id)
    assert result.session.campaign_name == "Saga"
    assert row["participants_json"]
    await result.finishing
    await mgr.stop(1, 2)


async def test_parts_are_numbered_without_stacking_suffixes(rig):
    mgr, db, _config, _bot = rig
    channel, _first = await start_game(rig, name="Heist (part 1)")

    second = await mgr.split(channel=channel)
    third = await mgr.split(channel=channel)

    assert second.session.name == "Heist (part 2)"
    assert third.session.name == "Heist (part 3)"
    assert third.previous_name == "Heist (part 2)"
    assert (await db.get_session(third.previous_id))["name"] == "Heist (part 2)"
    await asyncio.gather(second.finishing, third.finishing)
    await mgr.stop(1, 2)


async def test_offsets_in_the_new_part_start_from_its_own_start(rig):
    mgr, _db, _config, _bot = rig
    channel, _first = await start_game(rig)
    result = await mgr.split(channel=channel)
    channel.voice_client.speak(THORIN, 0.1)

    assert result.session.sink.offsets["10"] < 1.0
    await result.finishing
    await mgr.stop(1, 2)


async def test_a_speaker_heard_just_before_the_split_is_filed_with_the_old_part(rig):
    """Their first packet queues a registration; the part may close before it runs."""
    mgr, db, _config, _bot = rig
    channel, first = await start_game(rig, members=(THORIN,))
    channel.voice_client.speak(ELENYA, 0.2)  # not in the channel when it started

    result = await mgr.split(channel=channel)
    await result.finishing

    row = await db.get_session(first.session_id)
    assert "11" in row["offsets_json"]
    assert "11" in row["participants_json"]
    await mgr.stop(1, 2)


# -- nothing waits ----------------------------------------------------------------


async def test_the_split_does_not_wait_for_the_old_part_to_encode(rig, monkeypatch):
    mgr, db, _config, _bot = rig
    channel, first = await start_game(rig)
    channel.voice_client.speak(THORIN, 0.3)

    release = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    real = recorder.finalize_session_audio

    def slow(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=5)
        return real(*args, **kwargs)

    monkeypatch.setattr(recorder, "finalize_session_audio", slow)

    result = await asyncio.wait_for(mgr.split(channel=channel), timeout=2)
    await asyncio.wait_for(entered.wait(), timeout=2)

    assert not result.finishing.done(), "the old part must still be encoding"
    assert mgr.get(1, 2) is result.session, "...while the new one is already recording"
    channel.voice_client.speak(THORIN, 0.1)
    assert (await db.get_session(first.session_id))["completed"] == 0

    release.set()
    previous = await asyncio.wait_for(result.finishing, timeout=5)
    assert previous.enqueued
    await mgr.stop(1, 2)


async def test_a_new_session_can_start_while_the_last_one_is_still_encoding(rig, monkeypatch):
    """The stop used to hold the channel's lock through the encode."""
    mgr, _db, _config, _bot = rig
    channel, _first = await start_game(rig)
    channel.voice_client.speak(THORIN, 0.3)

    release = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    real = recorder.finalize_session_audio

    def slow(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=5)
        return real(*args, **kwargs)

    monkeypatch.setattr(recorder, "finalize_session_audio", slow)

    stopping = asyncio.create_task(mgr.stop(1, 2))
    await asyncio.wait_for(entered.wait(), timeout=2)

    channel.voice_client = FakeVoiceClient()  # a real connect() hands back a new client
    fresh = await asyncio.wait_for(
        mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name="Next"), timeout=2
    )
    assert not stopping.done()
    assert mgr.get(1, 2) is fresh

    release.set()
    assert (await asyncio.wait_for(stopping, timeout=5)).enqueued
    await mgr.stop(1, 2)


async def test_shutdown_waits_for_a_part_that_is_still_being_saved(rig, monkeypatch):
    mgr, db, _config, _bot = rig
    channel, first = await start_game(rig)
    channel.voice_client.speak(THORIN, 0.3)

    entered = threading.Event()
    real = recorder.finalize_session_audio

    def slow(*args, **kwargs):
        entered.set()
        threading.Event().wait(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(recorder, "finalize_session_audio", slow)
    await mgr.split(channel=channel)

    await mgr.shutdown_all()

    assert (await db.get_session(first.session_id))["completed"] == 1


async def test_stopping_the_new_part_while_the_old_one_is_saving(rig):
    mgr, _db, config, _bot = rig
    channel, first = await start_game(rig)
    channel.voice_client.speak(THORIN, 0.3)
    result = await mgr.split(channel=channel)
    channel.voice_client.speak(THORIN, 0.2)

    second = await mgr.stop(1, 2)
    previous = await result.finishing

    assert previous.enqueued and second.enqueued
    assert is_marked(config.outbox_dir / first.session_id, READY_MARKER)
    assert is_marked(config.outbox_dir / result.session.session_id, READY_MARKER)


# -- refusals leave the game alone --------------------------------------------------


async def test_nothing_to_split_is_reported(rig):
    mgr, _db, _config, _bot = rig
    with pytest.raises(RecordingError, match="Nothing is being recorded"):
        await mgr.split(channel=FakeChannel([THORIN]))


async def test_a_split_without_a_voice_connection_is_refused(rig):
    mgr, db, _config, _bot = rig
    channel, first = await start_game(rig)
    channel.voice_client._connected = False

    with pytest.raises(RecordingError, match="not connected"):
        await mgr.split(channel=channel)

    assert mgr.get(1, 2) is not None and mgr.get(1, 2).session_id == first.session_id
    await mgr.stop(1, 2)


async def test_a_split_the_disk_cannot_hold_is_refused_untouched(rig, monkeypatch):
    mgr, db, _config, _bot = rig
    channel, first = await start_game(rig)
    channel.voice_client.speak(THORIN, 0.2)
    monkeypatch.setattr("dnd_bot.capacity.free_bytes", lambda path: 1_000_000)

    with pytest.raises(RecordingError, match="Not enough disk space"):
        await mgr.split(channel=channel)

    assert mgr.get(1, 2).session_id == first.session_id
    assert channel.voice_client.recording
    cursor = await db.conn.execute("SELECT COUNT(*) FROM sessions")
    assert (await cursor.fetchone())[0] == 1, "no half-made second session"
    await mgr.stop(1, 2)


async def test_a_reader_that_refuses_the_swap_leaves_the_game_recording(rig):
    mgr, db, config, _bot = rig
    channel, first = await start_game(rig)
    channel.voice_client._reader.fail = True

    with pytest.raises(RecordingError, match="still recording"):
        await mgr.split(channel=channel)

    assert mgr.get(1, 2).session_id == first.session_id
    cursor = await db.conn.execute("SELECT COUNT(*) FROM sessions")
    assert (await cursor.fetchone())[0] == 1
    assert [p.name for p in config.sessions_dir.iterdir()] == [first.session_id]
    channel.voice_client.speak(THORIN, 0.2)
    result = await mgr.stop(1, 2)
    assert result.enqueued


async def test_a_library_without_set_sink_falls_back_to_restarting_the_recording(rig):
    mgr, _db, _config, _bot = rig
    channel, _first = await start_game(rig)
    channel.voice_client.has_set_sink = False
    channel.voice_client._reader = None

    result = await mgr.split(channel=channel)

    assert channel.voice_client.stops == 1
    assert channel.voice_client.recording
    assert channel.voice_client.sink is result.session.sink
    await result.finishing
    await mgr.stop(1, 2)


# -- a failure behind the scenes ------------------------------------------------------


async def test_a_part_that_cannot_be_saved_is_reported_and_the_new_one_is_untouched(
    rig, monkeypatch
):
    mgr, _db, _config, bot = rig
    channel, first = await start_game(rig)
    channel.voice_client.speak(THORIN, 0.2)

    def broken(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(recorder, "finalize_session_audio", broken)
    result = await mgr.split(channel=channel)

    assert await result.finishing is None
    assert any(first.session_id in line and "recover" in line for line in bot.text.sent)
    assert mgr.get(1, 2) is result.session
    assert channel.voice_client.recording
    assert first.session_id not in mgr._finalizing

    monkeypatch.undo()
    await mgr.stop(1, 2)


# -- the break reminder -----------------------------------------------------------------


async def test_the_break_reminder_is_posted_once(rig):
    mgr, _db, config, bot = rig
    mgr.config = replace(config, break_reminder_hours=0.00003)  # about a tenth of a second
    channel, session = await start_game(rig)

    await asyncio.sleep(0.4)

    assert len(bot.text.sent) == 1
    assert "/session split" in bot.text.sent[0]
    assert session.name in bot.text.sent[0]
    await mgr.stop(1, 2)


async def test_no_reminder_when_it_is_turned_off(rig):
    mgr, _db, _config, bot = rig
    channel, session = await start_game(rig)
    await asyncio.sleep(0.2)
    assert session.reminder_task is None
    assert bot.text.sent == []
    await mgr.stop(1, 2)


async def test_no_reminder_for_a_session_that_ended_first(rig):
    mgr, _db, config, bot = rig
    mgr.config = replace(config, break_reminder_hours=0.0003)  # about a second
    channel, session = await start_game(rig)

    await mgr.stop(1, 2)
    await asyncio.sleep(1.3)

    assert bot.text.sent == []
    assert session.reminder_task.cancelled() or session.reminder_task.done()


async def test_no_reminder_without_a_text_channel_to_post_in(rig):
    mgr, _db, config, bot = rig
    mgr.config = replace(config, break_reminder_hours=0.00003)
    channel = FakeChannel([THORIN])
    bot.voice = channel
    session = await mgr.start(channel=channel, text_channel_id=None, invoker=THORIN, name="Quiet")

    assert session.reminder_task is None
    await mgr.stop(1, 2)


async def test_the_new_part_gets_its_own_reminder_and_the_old_one_is_dropped(rig):
    mgr, _db, config, _bot = rig
    mgr.config = replace(config, break_reminder_hours=1.0)
    channel, first = await start_game(rig)
    old_reminder = first.reminder_task
    assert old_reminder is not None

    result = await mgr.split(channel=channel)
    await asyncio.sleep(0)

    assert old_reminder.cancelled()
    assert result.session.reminder_task is not None
    assert result.session.reminder_task is not old_reminder
    await result.finishing
    await mgr.stop(1, 2)


async def test_a_reminder_that_cannot_be_sent_does_not_disturb_the_recording(rig):
    mgr, _db, config, bot = rig
    mgr.config = replace(config, break_reminder_hours=0.00003)

    async def boom(content):
        raise RuntimeError("discord is down")

    bot.text.send = boom
    channel, session = await start_game(rig)
    await asyncio.sleep(0.4)

    assert channel.voice_client.recording
    assert mgr.get(1, 2) is session
    await mgr.stop(1, 2)


# -- the library this leans on ------------------------------------------------------------


def test_the_pinned_library_still_offers_the_reader_swap():
    """`_swap_sink` reaches for a non-public method; this is the tripwire."""
    reader_module = pytest.importorskip("discord.voice.receive.reader")
    import inspect

    reader = reader_module.AudioReader
    assert "sink" in inspect.signature(reader.set_sink).parameters
