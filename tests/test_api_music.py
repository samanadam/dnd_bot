"""Music routes: library, transport, queue, and how failures are reported.

The status codes matter more than the happy paths here. A yt-dlp outage must
read as 502 and a switched-off source as 503, so the portal can tell "YouTube
is broken again" from "you never turned it on".
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.music import MusicError, Track
from dnd_bot.tracks import SourceDisabled, TrackResolutionError

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeSource:
    def __init__(self, name="r2", tracks=None, error=None):
        self.name = name
        self.enabled = True
        self.tracks = tracks or [Track("music/tavern.opus", "tavern", name, "/cache/t.opus")]
        self.error = error

    async def browse(self, query=None, limit=50):
        if self.error:
            raise self.error
        tracks = self.tracks
        if query:
            tracks = [t for t in tracks if query.casefold() in t.title.casefold()]
        return tracks[:limit]

    async def resolve(self, track_id):
        if self.error:
            raise self.error
        for track in self.tracks:
            if track.id == track_id:
                return track
        raise TrackResolutionError("No such track.")


class FakeMusic:
    def __init__(self, sources):
        self.sources = sources
        self.calls = []
        self.error: Exception | None = None
        self.state = {
            "connected": True,
            "channel_id": "2",
            "owner": "music",
            "playing": True,
            "paused": False,
            "volume": 0.3,
            "loop": "off",
            "position_seconds": 0.0,
            "current": None,
            "queue": [],
            "sources": {"r2": True},
        }

    def state_summary(self, guild_id=None):
        return self.state

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.error:
            raise self.error

    async def play(self, guild_id, track, channel_id=None, position="end"):
        self._record("play", guild_id, track.id, channel_id, position)

    async def pause(self, guild_id):
        self._record("pause", guild_id)

    async def resume(self, guild_id):
        self._record("resume", guild_id)

    async def skip(self, guild_id):
        self._record("skip", guild_id)

    async def seek(self, guild_id, position):
        self._record("seek", guild_id, position)

    async def stop(self, guild_id):
        self._record("stop", guild_id)

    async def set_volume(self, guild_id, volume):
        self._record("set_volume", guild_id, volume)

    def set_loop(self, guild_id, mode):
        self._record("set_loop", guild_id, mode)

    def clear_queue(self, guild_id):
        self._record("clear_queue", guild_id)

    def remove(self, guild_id, index):
        self._record("remove", guild_id, index)

    def move(self, guild_id, source_index, target_index):
        self._record("move", guild_id, source_index, target_index)

    async def join(self, guild_id, channel_id):
        self._record("join", guild_id, channel_id)

    async def detach(self, guild_id, reason=""):
        self._record("detach", guild_id, reason)


@pytest.fixture
def source():
    return FakeSource()


@pytest.fixture
def music(source):
    return FakeMusic({"r2": source})


def make_client_factory(config: Config):
    async def build(music):
        config.ensure_dirs()
        bot = SimpleNamespace(
            config=replace(config, api_enabled=True, api_token=TOKEN, music_enabled=True),
            db=SimpleNamespace(pending_count=lambda: 0),
            manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
            store=None,
            music=music,
            is_ready=lambda: True,
        )
        return TestClient(TestServer(build_app(bot)))

    return build


@pytest.fixture
async def client(config: Config, music: FakeMusic):
    build = make_client_factory(config)
    async with await build(music) as test_client:
        test_client.music = music
        yield test_client


# -- state and library -------------------------------------------------------


async def test_state_reports_the_player(client):
    body = await (await client.get("/api/v1/music/state", headers=AUTH)).json()
    assert body["playing"] is True
    assert body["volume"] == 0.3


async def test_library_lists_tracks(client):
    body = await (await client.get("/api/v1/music/library", headers=AUTH)).json()
    assert body[0]["title"] == "tavern"
    # A library entry is a track, not a path on the bot's disk.
    assert "uri" not in body[0]


async def test_library_passes_a_query_through(client):
    body = await (await client.get("/api/v1/music/library?q=nothing", headers=AUTH)).json()
    assert body == []


async def test_an_unknown_source_is_503(client):
    response = await client.get("/api/v1/music/library?source=spotify", headers=AUTH)
    assert response.status == 503
    assert (await response.json())["error"]["code"] == "source_disabled"


async def test_a_bad_limit_is_400(client):
    assert (await client.get("/api/v1/music/library?limit=0", headers=AUTH)).status == 400


# -- playing -----------------------------------------------------------------


async def test_play_resolves_then_queues_the_track(client, music):
    response = await client.post(
        "/api/v1/music/play",
        json={"source": "r2", "id": "music/tavern.opus", "channel_id": "2"},
        headers=AUTH,
    )
    assert response.status == 202
    assert music.calls[0][0] == "play"
    assert music.calls[0][1] == (1, "music/tavern.opus", 2, "end")


async def test_play_requires_an_id(client):
    assert (await client.post("/api/v1/music/play", json={}, headers=AUTH)).status == 400


async def test_an_unresolvable_track_is_502(client, source):
    source.error = TrackResolutionError("Could not resolve that link.")
    response = await client.post(
        "/api/v1/music/play", json={"id": "music/tavern.opus"}, headers=AUTH
    )
    assert response.status == 502
    assert (await response.json())["error"]["code"] == "resolver_failed"


async def test_a_disabled_source_is_503(client, source):
    source.error = SourceDisabled("YouTube playback is turned off on this bot.")
    response = await client.post(
        "/api/v1/music/play", json={"id": "music/tavern.opus"}, headers=AUTH
    )
    assert response.status == 503


async def test_a_playback_conflict_is_409(client, music):
    music.error = MusicError("The queue is full (100 tracks).")
    response = await client.post(
        "/api/v1/music/play", json={"id": "music/tavern.opus"}, headers=AUTH
    )
    assert response.status == 409
    assert "queue is full" in (await response.json())["error"]["message"]


# -- transport ---------------------------------------------------------------


@pytest.mark.parametrize("action", ["pause", "resume", "skip", "stop"])
async def test_transport_actions_reach_the_player(client, music, action):
    assert (await client.post(f"/api/v1/music/{action}", headers=AUTH)).status == 200
    assert music.calls[0][0] == action


async def test_volume_is_forwarded(client, music):
    assert (
        await client.post("/api/v1/music/volume", json={"volume": 0.7}, headers=AUTH)
    ).status == 200
    assert music.calls[0][1] == (1, 0.7)


async def test_a_non_numeric_volume_is_400(client):
    response = await client.post("/api/v1/music/volume", json={"volume": "loud"}, headers=AUTH)
    assert response.status == 400


async def test_loop_mode_is_forwarded(client, music):
    await client.post("/api/v1/music/loop", json={"mode": "queue"}, headers=AUTH)
    assert music.calls[0][1] == (1, "queue")


async def test_a_rejected_loop_mode_is_409(client, music):
    music.error = MusicError("loop must be one of off, track, queue.")
    response = await client.post("/api/v1/music/loop", json={"mode": "sideways"}, headers=AUTH)
    assert response.status == 409


# -- queue -------------------------------------------------------------------


async def test_the_queue_can_be_cleared(client, music):
    assert (await client.delete("/api/v1/music/queue", headers=AUTH)).status == 200
    assert music.calls[0][0] == "clear_queue"


async def test_one_track_can_be_removed(client, music):
    assert (await client.delete("/api/v1/music/queue/2", headers=AUTH)).status == 200
    assert music.calls[0][1] == (1, 2)


async def test_a_track_can_be_moved(client, music):
    await client.post("/api/v1/music/queue/move", json={"from": 0, "to": 2}, headers=AUTH)
    assert music.calls[0][1] == (1, 0, 2)


async def test_a_malformed_move_is_400(client):
    response = await client.post("/api/v1/music/queue/move", json={"from": 0}, headers=AUTH)
    assert response.status == 400


# -- connection --------------------------------------------------------------


async def test_join_needs_a_channel(client):
    assert (await client.post("/api/v1/music/join", json={}, headers=AUTH)).status == 400


async def test_leave_detaches(client, music):
    assert (await client.post("/api/v1/music/leave", headers=AUTH)).status == 200
    assert music.calls[0][0] == "detach"


# -- music switched off ------------------------------------------------------


async def test_every_music_route_is_503_when_music_is_off(config):
    build = make_client_factory(config)
    async with await build(None) as client:
        for path in ("/api/v1/music/state", "/api/v1/music/library"):
            response = await client.get(path, headers=AUTH)
            assert response.status == 503, path
            assert (await response.json())["error"]["code"] == "music_disabled"


# -- auth --------------------------------------------------------------------


async def test_music_control_needs_the_token(client):
    assert (await client.post("/api/v1/music/stop")).status == 401


async def test_seek_validates_the_position_and_reaches_the_player(client, music):
    ok = await client.post("/api/v1/music/seek", json={"position_seconds": 42.5}, headers=AUTH)
    assert ok.status == 200
    assert ("seek", (1, 42.5), {}) in music.calls
    for body in (
        {},
        {"position_seconds": "5"},
        {"position_seconds": True},
        {"position_seconds": None},
    ):
        bad = await client.post("/api/v1/music/seek", json=body, headers=AUTH)
        assert bad.status == 400


async def test_seek_past_the_end_reads_as_a_conflict(client, music):
    music.error = MusicError("That is past the end of the track.")
    response = await client.post(
        "/api/v1/music/seek", json={"position_seconds": 99999}, headers=AUTH
    )
    assert response.status == 409
