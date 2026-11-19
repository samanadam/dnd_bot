"""Live casts: the jitter buffer, tickets, the socket gatekeeping, and a real
webm stream decoded by ffmpeg into frames the mixer can play."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app
from dnd_bot.cast import CAP_FRAMES, TARGET_FRAMES, CastManager, CastSource
from dnd_bot.config import Config
from dnd_bot.mixer import FRAME_BYTES, SILENCE

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "https://portal.example"

# -- the buffer --------------------------------------------------------------


def test_underrun_gives_silence_never_a_short_frame():
    source = CastSource()
    assert source.read() == SILENCE
    source.feed(b"\x01" * (FRAME_BYTES // 2))
    assert source.read() == SILENCE


def test_plays_only_after_the_target_is_buffered():
    source = CastSource()
    source.feed(b"\x01" * FRAME_BYTES * (TARGET_FRAMES - 1))
    assert source.read() == SILENCE
    source.feed(b"\x01" * FRAME_BYTES)
    assert source.read() == b"\x01" * FRAME_BYTES


def test_overflow_drops_the_oldest_audio():
    source = CastSource()
    source.feed(b"\x01" * FRAME_BYTES * CAP_FRAMES)
    source.feed(b"\x02" * FRAME_BYTES * 2)
    assert len(source._buffer) == TARGET_FRAMES * FRAME_BYTES
    assert source._buffer.endswith(b"\x02" * FRAME_BYTES * 2)


def test_finish_ends_the_track_and_cleanup_closes_it():
    source = CastSource()
    source.feed(b"\x01" * FRAME_BYTES * TARGET_FRAMES)
    source.finish()
    assert source.read() == b""
    other = CastSource()
    other.cleanup()
    assert other.read() == b"" and other.closed


# -- tickets -----------------------------------------------------------------


async def test_a_ticket_works_once():
    cast = CastManager()
    session, ticket = cast.create("Aria")
    assert cast.redeem(ticket) is session
    assert cast.redeem(ticket) is None


async def test_an_expired_ticket_is_refused():
    now = [0.0]
    cast = CastManager(clock=lambda: now[0])
    _, ticket = cast.create("Aria")
    now[0] = 31.0
    assert cast.redeem(ticket) is None


async def test_an_unknown_ticket_is_refused():
    assert CastManager().redeem("nope") is None


async def test_ending_a_cast_voids_its_ticket_and_names_it_in_the_summary():
    cast = CastManager()
    session, ticket = cast.create("A" * 100)
    assert len(cast.summary()["by_name"]) == 40
    cast.end(session)
    assert cast.summary() is None
    assert cast.redeem(ticket) is None


async def test_a_new_cast_replaces_the_active_one_in_the_summary():
    cast = CastManager()
    first, _ = cast.create("Aria")
    second, _ = cast.create("Bo")
    assert cast.summary()["by_name"] == "Bo"
    cast.end(first)
    assert cast.summary()["by_name"] == "Bo"
    assert cast.source_for(second.id) is second.source


# -- routes ------------------------------------------------------------------


class FakeMusic:
    def __init__(self, cast):
        self.cast = cast
        self.played = []
        self.sources = {}

    async def play(self, guild_id, track, channel_id=None, position="end"):
        self.played.append((track.source, track.title, position))

    def state_summary(self, guild_id=None):
        return {}


async def _client(config: Config, *, cast_on: bool):
    config.ensure_dirs()
    cast = CastManager() if cast_on else None
    music = FakeMusic(cast)
    bot = SimpleNamespace(
        config=replace(
            config,
            api_enabled=True,
            api_token=TOKEN,
            music_enabled=True,
            api_cors_origins=(ORIGIN,),
        ),
        db=SimpleNamespace(pending_count=lambda: 0),
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=music,
        is_ready=lambda: True,
    )
    client = TestClient(TestServer(build_app(bot)))
    await client.start_server()
    client.music = music
    return client


@pytest.fixture
async def client(config: Config):
    test_client = await _client(config, cast_on=True)
    yield test_client
    await test_client.close()


async def test_routes_are_404_while_casting_is_off(config: Config):
    off = await _client(config, cast_on=False)
    try:
        start = await off.post("/api/v1/cast/start", json={"name": "A"}, headers=AUTH)
        assert start.status == 404
        assert (await off.post("/api/v1/cast/stop", headers=AUTH)).status == 404
    finally:
        await off.close()


async def test_start_needs_the_bearer_token(client):
    assert (await client.post("/api/v1/cast/start", json={"name": "A"})).status == 401


async def test_start_needs_a_name(client):
    assert (await client.post("/api/v1/cast/start", json={}, headers=AUTH)).status == 400


async def test_start_plays_a_cast_track_now_and_returns_a_ticket(client):
    response = await client.post("/api/v1/cast/start", json={"name": "Aria"}, headers=AUTH)
    assert response.status == 202
    body = await response.json()
    assert body["expires_in"] == 30 and len(body["ticket"]) >= 32
    assert client.music.played == [("cast", "Live: Aria", "now")]


async def test_socket_refuses_a_foreign_origin(client):
    response = await client.get("/api/v1/cast/ws", headers={"Origin": "https://evil.example"})
    assert response.status == 403
    assert (await client.get("/api/v1/cast/ws")).status == 403


async def test_socket_refuses_a_bad_ticket(client):
    ws = await client.ws_connect("/api/v1/cast/ws", headers={"Origin": ORIGIN})
    await ws.send_str("not-a-ticket")
    message = await ws.receive(timeout=2)
    assert message.type == WSMsgType.CLOSE and message.data == 4003
    await ws.close()


async def test_socket_closes_when_no_ticket_is_sent(client, monkeypatch):
    monkeypatch.setattr("dnd_bot.api.routes_cast.FIRST_MESSAGE_SECONDS", 0.2)
    ws = await client.ws_connect("/api/v1/cast/ws", headers={"Origin": ORIGIN})
    message = await ws.receive(timeout=2)
    assert message.type == WSMsgType.CLOSE and message.data == 4001
    await ws.close()


async def test_stop_ends_the_active_cast(client):
    await client.post("/api/v1/cast/start", json={"name": "Aria"}, headers=AUTH)
    body = await (await client.post("/api/v1/cast/stop", headers=AUTH)).json()
    assert body == {"stopped": True}
    body = await (await client.post("/api/v1/cast/stop", headers=AUTH)).json()
    assert body == {"stopped": False}


# -- the real thing: webm in, PCM frames out ---------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_a_webm_stream_becomes_pcm_frames(client):
    start = await (
        await client.post("/api/v1/cast/start", json={"name": "Aria"}, headers=AUTH)
    ).json()
    webm = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "libopus",
            "-f",
            "webm",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout

    ws = await client.ws_connect("/api/v1/cast/ws", headers={"Origin": ORIGIN})
    await ws.send_str(start["ticket"])
    assert (await ws.receive(timeout=2)).data == "ready"
    source = client.music.cast.source_for(start["cast_id"])
    for i in range(0, len(webm), 4000):
        await ws.send_bytes(webm[i : i + 4000])
        await asyncio.sleep(0.01)

    loud = 0
    for _ in range(200):
        frame = source.read()
        if frame and frame != SILENCE:
            loud += 1
        if loud >= 10:
            break
        await asyncio.sleep(0.02)
    await ws.close()
    assert loud >= 10, "decoded audio never reached the source"
