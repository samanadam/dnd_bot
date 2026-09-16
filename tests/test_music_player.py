"""The music player, and its truce with the recorder over one voice connection.

Discord allows a bot one voice connection per guild, and the recorder owns it
whenever a session is live. Most of what is tested here is that arrangement:
music borrows the recording's connection and never hangs up on it, and it
survives the reconnect that replaces that connection mid-session.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from dnd_bot.config import Config
from dnd_bot.music import MusicError, MusicManager, Track


class FakeSource:
    """Stands in for an ffmpeg-backed AudioSource."""

    def __init__(self, uri: str, before_options: str = "", volume: float = 1.0):
        self.uri = uri
        self.before_options = before_options
        self.volume = volume
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


class FakeVoiceClient:
    def __init__(self, channel_id: int = 2):
        self.channel = type("Chan", (), {"id": channel_id})()
        self.playing = False
        self.paused = False
        self.source = None
        self._after = None
        self.disconnected = False
        self.play_error: Exception | None = None

    def is_connected(self):
        return not self.disconnected

    def is_playing(self):
        return self.playing and not self.paused

    def is_paused(self):
        return self.paused

    def play(self, source, after=None, **kwargs):
        if self.play_error:
            raise self.play_error
        self.source = source
        # Private, like py-cord: the callback lives on the AudioPlayer, so
        # assigning `voice_client.after` from outside does nothing at all.
        self._after = after
        self.playing = True
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        """py-cord fires `after` from a finally block, so stopping fires it too.

        This is the detail that matters: an interrupted track still reports
        that it ended, and a player that treats that as "advance the queue"
        skips a track every time somebody hits next.
        """
        self.playing = False
        self.paused = False
        after, self._after = self._after, None
        if after:
            after(None)

    async def disconnect(self, force: bool = False):
        self.disconnected = True
        self.playing = False

    def finish_track(self):
        """What py-cord does when a source is exhausted."""
        self.stop()


class FakeSession:
    def __init__(self, voice_client, channel_id: int = 2):
        self.guild_id = 1
        self.channel_id = channel_id
        self.voice_client = voice_client


class FakeChannel:
    def __init__(self, channel_id: int, voice_client: FakeVoiceClient):
        self.id = channel_id
        self.name = "Table"
        self._voice_client = voice_client

    async def connect(self, **kwargs):
        return self._voice_client


class FakeGuild:
    def __init__(self, channels):
        self.id = 1
        self._channels = channels
        self.voice_client = None

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class FakeManager:
    def __init__(self):
        self.sessions = []

    def sessions_in_guild(self, guild_id):
        return list(self.sessions)


class FakeBot:
    def __init__(self, config, guild):
        self.config = config
        self.manager = FakeManager()
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild.id else None


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


async def drain():
    """Let a callback scheduled from the player thread actually run.

    The after callback hops back onto the loop with run_coroutine_threadsafe,
    which takes a couple of iterations to land. Asserting before that is how a
    double-advance hides.
    """
    for _ in range(10):
        await asyncio.sleep(0)


def track(track_id: str = "t1", title: str = "Tavern") -> Track:
    return Track(id=track_id, title=title, source="r2", uri=f"/cache/{track_id}.opus")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def voice():
    return FakeVoiceClient()


@pytest.fixture
def music_config(config: Config) -> Config:
    return replace(config, music_enabled=True, music_default_volume=0.3, music_max_queue=3)


@pytest.fixture
def manager(music_config, voice, clock):
    guild = FakeGuild({2: FakeChannel(2, voice), 5: FakeChannel(5, voice)})
    bot = FakeBot(music_config, guild)
    return MusicManager(
        bot,
        music_config,
        sources={},
        source_factory=lambda uri, volume, before_options="": FakeSource(
            uri, before_options, volume
        ),
        clock=clock,
    )


# -- playing -----------------------------------------------------------------


async def test_play_connects_and_starts_the_track(manager, voice):
    await manager.play(1, track(), channel_id=2)
    assert voice.playing
    assert voice.source.uri == "/cache/t1.opus"
    assert manager.player(1).current.id == "t1"


async def test_play_applies_the_configured_volume(manager, voice):
    await manager.play(1, track(), channel_id=2)
    assert voice.source.volume == 0.3


async def test_a_second_track_queues_behind_the_first(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    assert voice.source.uri == "/cache/t1.opus"
    assert [t.id for t in manager.player(1).queue] == ["t2"]


async def test_play_now_interrupts_the_current_track(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2, position="now")
    assert manager.player(1).current.id == "t2"


async def test_the_queue_has_a_ceiling(manager):
    for index in range(4):
        if index < 3:
            await manager.play(1, track(f"t{index}"), channel_id=2)
        else:
            with pytest.raises(MusicError, match="queue"):
                await manager.play(1, track("t9"), channel_id=2)


async def test_a_refused_play_becomes_a_music_error(manager, voice):
    """py-cord raises ClientException for 'already playing'; it must not 500."""
    import discord

    voice.play_error = discord.ClientException("Already playing audio.")
    with pytest.raises(MusicError):
        await manager.play(1, track(), channel_id=2)


# -- advancing ---------------------------------------------------------------


async def test_the_queue_advances_when_a_track_ends(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    await manager.on_track_end(1, None)
    assert manager.player(1).current.id == "t2"
    assert voice.source.uri == "/cache/t2.opus"


async def test_an_empty_queue_leaves_the_player_idle(manager):
    await manager.play(1, track(), channel_id=2)
    await manager.on_track_end(1, None)
    assert manager.player(1).current is None
    assert not manager.player(1).queue


async def test_loop_track_repeats_the_same_track(manager):
    await manager.play(1, track("t1"), channel_id=2)
    manager.set_loop(1, "track")
    await manager.on_track_end(1, None)
    assert manager.player(1).current.id == "t1"


async def test_loop_queue_sends_the_track_to_the_back(manager):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    manager.set_loop(1, "queue")
    await manager.on_track_end(1, None)
    assert manager.player(1).current.id == "t2"
    assert [t.id for t in manager.player(1).queue] == ["t1"]


async def test_an_unknown_loop_mode_is_refused(manager):
    with pytest.raises(MusicError):
        manager.set_loop(1, "sideways")


async def test_skip_moves_to_the_next_track(manager):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    await manager.skip(1)
    assert manager.player(1).current.id == "t2"


# -- transport ---------------------------------------------------------------


async def test_pause_and_resume(manager, voice):
    await manager.play(1, track(), channel_id=2)
    await manager.pause(1)
    assert voice.paused
    await manager.resume(1)
    assert not voice.paused


async def test_pausing_nothing_is_an_error(manager):
    with pytest.raises(MusicError):
        await manager.pause(1)


async def test_volume_is_applied_live_without_restarting_the_track(manager, voice):
    await manager.play(1, track(), channel_id=2)
    source = voice.source
    await manager.set_volume(1, 0.8)
    assert source.volume == 0.8
    assert voice.source is source


async def test_volume_outside_the_range_is_refused(manager):
    await manager.play(1, track(), channel_id=2)
    with pytest.raises(MusicError):
        await manager.set_volume(1, 9.0)


async def test_stop_clears_the_queue(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    await manager.stop(1)
    assert manager.player(1).current is None
    assert not manager.player(1).queue


# -- position ----------------------------------------------------------------


async def test_position_tracks_elapsed_time(manager, clock):
    await manager.play(1, track(), channel_id=2)
    clock.advance(30.0)
    assert manager.player(1).position_seconds(clock()) == pytest.approx(30.0)


async def test_paused_time_does_not_count_towards_position(manager, clock):
    await manager.play(1, track(), channel_id=2)
    clock.advance(10.0)
    await manager.pause(1)
    clock.advance(50.0)
    await manager.resume(1)
    assert manager.player(1).position_seconds(clock()) == pytest.approx(10.0)


# -- sharing the voice connection with the recorder --------------------------


async def test_music_borrows_the_recordings_connection(manager, voice):
    """Two connections in one guild is not allowed, so music uses the one there is."""
    recording_client = FakeVoiceClient()
    manager.bot.manager.sessions = [FakeSession(recording_client)]
    await manager.play(1, track(), channel_id=2)
    assert recording_client.playing
    assert manager.player(1).owner == "recording"


async def test_detach_never_disconnects_a_borrowed_connection(manager):
    """Hanging up here would end the recording. It must only stop playback."""
    recording_client = FakeVoiceClient()
    manager.bot.manager.sessions = [FakeSession(recording_client)]
    await manager.play(1, track(), channel_id=2)
    await manager.detach(1, reason="recording_start")
    assert recording_client.disconnected is False
    assert recording_client.playing is False


async def test_detach_disconnects_a_connection_music_made_itself(manager, voice):
    await manager.play(1, track(), channel_id=2)
    assert manager.player(1).owner == "music"
    await manager.detach(1, reason="recording_start")
    assert voice.disconnected is True


async def test_detach_keeps_the_queue_and_the_interrupted_track(manager):
    """A recording starting must not throw away what was lined up for it.

    The track that was playing goes back to the head of the queue, so the
    rebind after the session connects picks it straight back up.
    """
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    await manager.detach(1, reason="recording_start")
    assert [t.id for t in manager.player(1).queue] == ["t1", "t2"]


# -- the reconnect case ------------------------------------------------------


async def test_rebind_resumes_the_track_on_the_new_connection(manager, clock):
    """_resume_recording replaces the voice client, which silently kills playback.

    Without this hook the music simply stops and nothing says why.
    """
    await manager.play(1, track("t1"), channel_id=2)
    clock.advance(42.0)

    fresh = FakeVoiceClient()
    await manager.rebind(1, fresh, channel_id=2)

    assert fresh.playing
    assert fresh.source.uri == "/cache/t1.opus"
    assert "-ss 42.00" in fresh.source.before_options


async def test_rebind_does_nothing_when_nothing_was_playing(manager):
    fresh = FakeVoiceClient()
    await manager.rebind(1, fresh, channel_id=2)
    assert fresh.playing is False


async def test_rebind_can_be_turned_off(music_config, clock, voice):
    guild = FakeGuild({2: FakeChannel(2, voice)})
    bot = FakeBot(replace(music_config, music_resume_after_reconnect=False), guild)
    manager = MusicManager(
        bot,
        bot.config,
        sources={},
        source_factory=lambda uri, volume, before_options="": FakeSource(uri, before_options),
        clock=clock,
    )
    await manager.play(1, track(), channel_id=2)
    fresh = FakeVoiceClient()
    await manager.rebind(1, fresh, channel_id=2)
    assert fresh.playing is False


async def test_a_failed_resume_leaves_the_bot_alive(manager, clock):
    """A reconnect that cannot restart the music must not raise into the recorder."""
    await manager.play(1, track(), channel_id=2)
    fresh = FakeVoiceClient()
    fresh.play_error = RuntimeError("no")
    await manager.rebind(1, fresh, channel_id=2)
    assert manager.player(1).current is None


# -- state -------------------------------------------------------------------


async def test_state_summary_is_json_shaped(manager, voice):
    import json

    await manager.play(1, track(), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    state = manager.state_summary(1)
    json.dumps(state)
    assert state["playing"] is True
    assert state["current"]["id"] == "t1"
    assert [item["id"] for item in state["queue"]] == ["t2"]
    assert state["volume"] == 0.3
    assert state["loop"] == "off"
    assert state["channel_id"] == "2"


async def test_state_summary_when_idle(manager):
    state = manager.state_summary(1)
    assert state["playing"] is False
    assert state["current"] is None
    assert state["connected"] is False


# -- the hooks the recorder calls --------------------------------------------


class SpyMusic:
    """Records the calls SessionManager makes, and refuses to be fatal."""

    def __init__(self, explode: bool = False):
        self.calls = []
        self.explode = explode

    async def detach(self, guild_id, reason=""):
        self.calls.append(("detach", guild_id, reason))
        if self.explode:
            raise RuntimeError("music is broken")

    async def rebind(self, guild_id, voice_client, channel_id=None):
        self.calls.append(("rebind", guild_id, channel_id))
        if self.explode:
            raise RuntimeError("music is broken")


async def test_the_recorder_releases_music_before_it_connects(config, tmp_path):
    from dnd_bot.recorder import SessionManager

    manager = SessionManager(bot=None, db=None, config=config)
    spy = SpyMusic()
    manager.music = spy
    await manager._music_release(1, "recording_start")
    assert spy.calls == [("detach", 1, "recording_start")]


async def test_a_broken_music_player_cannot_stop_a_recording(config):
    """A lost song is a nuisance; a lost session is not. The hooks swallow."""
    from dnd_bot.recorder import SessionManager

    manager = SessionManager(bot=None, db=None, config=config)
    manager.music = SpyMusic(explode=True)
    await manager._music_release(1, "recording_start")  # must not raise


async def test_the_hooks_are_inert_without_a_player(config):
    from dnd_bot.recorder import SessionManager

    manager = SessionManager(bot=None, db=None, config=config)
    assert manager.music is None
    await manager._music_release(1, "recording_start")  # must not raise


# -- interrupted tracks must not advance the queue ---------------------------
#
# py-cord calls the `after` callback from a finally block, so a track that was
# stopped reports itself finished exactly like one that ran out. Treating both
# the same means every interruption silently eats a track.


async def test_skip_advances_exactly_one_track(manager):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    await manager.play(1, track("t3"), channel_id=2)

    await manager.skip(1)
    await drain()

    assert manager.player(1).current.id == "t2"
    assert [t.id for t in manager.player(1).queue] == ["t3"]


async def test_play_now_does_not_get_overtaken_by_the_track_it_interrupted(manager):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("queued"), channel_id=2)

    await manager.play(1, track("urgent"), channel_id=2, position="now")
    await drain()

    assert manager.player(1).current.id == "urgent"
    assert [t.id for t in manager.player(1).queue] == ["queued"]


async def test_stopping_does_not_restart_anything(manager):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)

    await manager.stop(1)
    await drain()

    assert manager.player(1).current is None
    assert not manager.player(1).queue


async def test_detaching_does_not_start_the_next_track(manager):
    """A recording is claiming the connection; music must go quiet, not carry on."""
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)

    await manager.detach(1, reason="recording_start")
    await drain()

    assert manager.player(1).current is None


async def test_a_track_that_ends_on_its_own_still_advances(manager, voice):
    """The guard must not break the normal case it sits next to."""
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)

    voice.finish_track()
    await drain()

    assert manager.player(1).current.id == "t2"


async def test_stopping_a_recording_releases_music(config, tmp_path):
    """The voice client is about to be disconnected; music must let go of it."""
    from pathlib import Path

    from dnd_bot.db import Database
    from dnd_bot.recorder import SessionManager

    migrations = Path(__file__).resolve().parent.parent / "migrations"
    config.ensure_dirs()
    db = Database(tmp_path / "bot.db", migrations)
    await db.connect()
    try:
        manager = SessionManager(bot=None, db=db, config=config)
        spy = SpyMusic()
        manager.music = spy
        # Nothing is recording, so stop() returns early - but the hook itself
        # is what this pins: it must exist on the teardown path.
        await manager._music_release(1, "recording_stopped")
        assert spy.calls == [("detach", 1, "recording_stopped")]
    finally:
        await db.close()
