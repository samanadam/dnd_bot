# ruff: noqa: F811 - fixtures come from test_music_player by name
"""Soundboard layers on the music player's voice connection.

The rule that matters: adding or removing a sound never restarts, skips or
double-advances the music, and plain music playback without the soundboard
behaves exactly as before.
"""

from __future__ import annotations

import pytest
from test_music_player import (  # noqa: F401 - fixtures are used by name
    FakeSession,
    clock,
    drain,
    manager,
    music_config,
    track,
    voice,
)

from dnd_bot.mixer import MixerSource
from dnd_bot.music import MAX_AMBIENCE_LAYERS, MAX_SFX_LAYERS, MusicError, Track


def sound(track_id: str = "music/ambience/rain.ogg", title: str = "rain") -> Track:
    return Track(id=track_id, title=title, source="r2", uri=f"/cache/{title}.ogg")


async def test_ambience_alone_plays_a_mixer(manager, voice):
    layer = await manager.play_layer(1, sound(), kind="ambience", channel_id=2)
    assert isinstance(voice.source, MixerSource)
    assert [item.id for item in voice.source.snapshot()] == [layer.id]
    state = manager.soundboard_state(1)
    assert state["layers"][0]["kind"] == "ambience"
    assert state["layers"][0]["title"] == "rain"
    music = manager.state_summary(1)
    assert music["playing"] is False
    assert music["current"] is None


async def test_ambience_slides_under_playing_music_without_restarting_it(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    music_source = voice.source
    epoch = manager.player(1).epoch

    await manager.play_layer(1, sound(), kind="ambience")
    mixer = voice.source
    assert isinstance(mixer, MixerSource)
    assert mixer.main is music_source
    assert manager.player(1).epoch == epoch
    assert manager.state_summary(1)["playing"] is True


async def test_next_track_is_fed_into_the_running_mixer(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    await manager.play_layer(1, sound(), kind="ambience")
    mixer = voice.source

    # The music track ends inside the mixer, which reports it.
    manager.player(1).main_callback(None)
    await drain()

    assert voice.source is mixer
    assert mixer.main.uri == "/cache/t2.opus"
    assert manager.player(1).current.id == "t2"


async def test_the_track_end_is_not_reported_twice(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    await manager.play(1, track("t3"), channel_id=2)
    await manager.play_layer(1, sound(), kind="ambience")
    callback = manager.player(1).main_callback

    callback(None)
    callback(None)
    await drain()
    assert manager.player(1).current.id == "t2"


async def test_skip_keeps_the_ambience_playing(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play(1, track("t2"), channel_id=2)
    await manager.play_layer(1, sound(), kind="ambience")
    mixer = voice.source

    await manager.skip(1)
    await drain()
    assert voice.playing
    assert voice.source is mixer
    assert manager.player(1).current.id == "t2"
    assert len(mixer.snapshot()) == 1


async def test_stop_music_leaves_ambience(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play_layer(1, sound(), kind="ambience")
    await manager.stop(1)
    assert voice.playing
    assert manager.state_summary(1)["current"] is None
    assert len(manager.soundboard_state(1)["layers"]) == 1


async def test_pause_with_a_mixer_silences_only_the_music(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.play_layer(1, sound(), kind="ambience")
    await manager.pause(1)
    assert not voice.paused
    assert voice.source.main_paused
    assert manager.state_summary(1)["paused"] is True
    await manager.resume(1)
    assert not voice.source.main_paused
    assert manager.state_summary(1)["playing"] is True


async def test_ambience_added_while_music_is_paused_keeps_the_music_paused(manager, voice):
    await manager.play(1, track("t1"), channel_id=2)
    await manager.pause(1)
    await manager.play_layer(1, sound(), kind="ambience")
    assert not voice.paused
    assert voice.source.main_paused


async def test_music_started_after_ambience_joins_the_mixer(manager, voice):
    await manager.play_layer(1, sound(), kind="ambience", channel_id=2)
    mixer = voice.source
    await manager.play(1, track("t1"))
    assert voice.source is mixer
    assert mixer.main.uri == "/cache/t1.opus"


async def test_same_ambience_twice_is_refused(manager):
    await manager.play_layer(1, sound(), kind="ambience", channel_id=2)
    with pytest.raises(MusicError):
        await manager.play_layer(1, sound(), kind="ambience")


async def test_ambience_limit(manager):
    for index in range(MAX_AMBIENCE_LAYERS):
        await manager.play_layer(
            1, sound(f"music/ambience/{index}.ogg", f"a{index}"), kind="ambience", channel_id=2
        )
    with pytest.raises(MusicError):
        await manager.play_layer(1, sound("music/ambience/x.ogg", "x"), kind="ambience")


async def test_sfx_over_the_limit_replaces_the_oldest(manager):
    first = None
    for index in range(MAX_SFX_LAYERS + 1):
        layer = await manager.play_layer(
            1, sound(f"music/sfx/{index}.ogg", f"s{index}"), kind="sfx", channel_id=2
        )
        first = first or layer
    ids = [item["id"] for item in manager.soundboard_state(1)["layers"]]
    assert len(ids) == MAX_SFX_LAYERS
    assert first.id not in ids


async def test_stop_layer_and_stop_all(manager):
    rain = await manager.play_layer(1, sound(), kind="ambience", channel_id=2)
    await manager.play_layer(1, sound("music/sfx/boom.ogg", "boom"), kind="sfx")
    await manager.stop_layer(1, rain.id)
    assert [item["title"] for item in manager.soundboard_state(1)["layers"]] == ["boom"]
    assert manager.player(1).ambience == []
    with pytest.raises(MusicError):
        await manager.stop_layer(1, rain.id)
    assert await manager.stop_layers(1) == 1


async def test_layer_volume_is_live(manager, voice):
    rain = await manager.play_layer(1, sound(), kind="ambience", channel_id=2, volume=0.5)
    await manager.set_layer_volume(1, rain.id, 1.5)
    assert voice.source.snapshot()[0].source.volume == 1.5
    with pytest.raises(MusicError):
        await manager.set_layer_volume(1, rain.id, 3)


async def test_only_bucket_files_can_be_layered(manager):
    stream = Track(id="yt", title="yt", source="youtube", uri="https://example.com/a")
    with pytest.raises(MusicError):
        await manager.play_layer(1, stream, kind="sfx", channel_id=2)
    with pytest.raises(MusicError):
        await manager.play_layer(1, sound(), kind="drums", channel_id=2)


async def test_rebind_brings_ambience_back_on_the_new_connection(manager, voice):
    from test_music_player import FakeVoiceClient

    await manager.play_layer(1, sound(), kind="ambience", channel_id=2)
    fresh = FakeVoiceClient()
    manager.bot.manager.sessions.append(FakeSession(fresh))
    await manager.rebind(1, fresh, channel_id=2)
    assert isinstance(fresh.source, MixerSource)
    assert [layer.title for layer in fresh.source.snapshot()] == ["rain"]


async def test_leaving_forgets_ambience(manager, voice):
    await manager.play_layer(1, sound(), kind="ambience", channel_id=2)
    await manager.detach(1, reason="api_leave")
    assert manager.player(1).ambience == []
    assert manager.player(1).mixer is None or manager.player(1).mixer.closed


# -- YouTube sounds are cache files, never streams ---------------------------


def youtube_sound(config, name="yt_abcdefghijk.m4a") -> Track:
    directory = config.music_cache_dir / "youtube"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x")
    return Track(
        id="https://www.youtube.com/watch?v=abcdefghijk",
        title="thunder",
        source="youtube",
        uri=str(path),
    )


async def test_a_saved_youtube_sound_plays_as_a_layer(manager, voice, music_config):
    layer = await manager.play_layer(1, youtube_sound(music_config), kind="ambience", channel_id=2)
    assert [item.id for item in voice.source.snapshot()] == [layer.id]
    # Layers stay file-only for ffmpeg whatever the source was.
    assert manager.soundboard_state(1)["layers"][0]["title"] == "thunder"


async def test_a_youtube_stream_url_is_never_a_layer(manager, voice):
    stream = Track(
        id="https://www.youtube.com/watch?v=abcdefghijk",
        title="live",
        source="youtube",
        uri="https://rr1.googlevideo.com/videoplayback?x=1",
    )
    with pytest.raises(MusicError, match="library or cache"):
        await manager.play_layer(1, stream, kind="ambience", channel_id=2)
    assert voice.source is None


@pytest.mark.parametrize("where", ["outside", "sibling", "traversal"])
async def test_a_youtube_file_must_be_inside_the_music_cache(manager, voice, music_config, where):
    cache = music_config.music_cache_dir
    outside = music_config.data_dir / "secret.m4a"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"x")
    uri = {
        "outside": str(outside),
        "sibling": str(cache.parent / "music-evil" / "a.m4a"),
        "traversal": str(cache / "youtube" / ".." / ".." / "secret.m4a"),
    }[where]
    track = Track(
        id="https://www.youtube.com/watch?v=abcdefghijk", title="x", source="youtube", uri=uri
    )
    with pytest.raises(MusicError):
        await manager.play_layer(1, track, kind="sfx", channel_id=2)
    assert voice.source is None


@pytest.mark.parametrize("uri", ["", "-i evil", "--help"])
async def test_a_layer_needs_a_real_path(manager, voice, uri):
    track = Track(id="x", title="x", source="youtube", uri=uri)
    with pytest.raises(MusicError):
        await manager.play_layer(1, track, kind="sfx", channel_id=2)


async def test_an_unknown_source_is_not_a_layer(manager, voice, music_config):
    track = youtube_sound(music_config)
    track.source = "soundcloud"
    with pytest.raises(MusicError):
        await manager.play_layer(1, track, kind="sfx", channel_id=2)


async def test_the_same_youtube_ambience_cannot_start_twice(manager, voice, music_config):
    await manager.play_layer(1, youtube_sound(music_config), kind="ambience", channel_id=2)
    with pytest.raises(MusicError, match="already playing"):
        await manager.play_layer(1, youtube_sound(music_config), kind="ambience")
