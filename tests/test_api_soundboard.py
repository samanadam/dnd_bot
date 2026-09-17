"""Soundboard routes: every id is checked against its folder before playing."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.music import MusicError, Track

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeSource:
    name = "r2"
    enabled = True

    def __init__(self):
        self.folders = {
            "ambience": [Track("music/ambience/rain.ogg", "rain", "r2", "")],
            "sfx": [Track("music/sfx/door.ogg", "door", "r2", "")],
        }

    async def browse_folder(self, folder, limit=200):
        return list(self.folders[folder])

    async def resolve(self, track_id):
        return Track(track_id, track_id.rsplit("/", 1)[-1], "r2", "/cache/x.ogg")


class FakeMusic:
    def __init__(self):
        self.sources = {"r2": FakeSource()}
        self.calls = []
        self.error = None

    def soundboard_state(self, guild_id=None):
        return {"connected": True, "layers": [], "limits": {"ambience": 3, "sfx": 6}}

    async def play_layer(self, guild_id, track, *, kind, volume=1.0, channel_id=None):
        if self.error:
            raise self.error
        self.calls.append(("play", track.id, kind, volume, channel_id))

    async def stop_layer(self, guild_id, layer_id):
        self.calls.append(("stop_layer", layer_id))

    async def stop_layers(self, guild_id, kind=None):
        self.calls.append(("stop_layers", kind))
        return 0

    async def set_layer_volume(self, guild_id, layer_id, volume):
        self.calls.append(("volume", layer_id, volume))


@pytest.fixture
async def client(config: Config):
    config.ensure_dirs()
    music = FakeMusic()
    bot = SimpleNamespace(
        config=replace(config, api_enabled=True, api_token=TOKEN, music_enabled=True),
        db=SimpleNamespace(pending_count=lambda: 0),
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=music,
        is_ready=lambda: True,
    )
    async with TestClient(TestServer(build_app(bot))) as test_client:
        test_client.music = music
        yield test_client


async def test_library_groups_sounds(client):
    body = await (await client.get("/api/v1/soundboard", headers=AUTH)).json()
    assert [s["title"] for s in body["ambience"]] == ["rain"]
    assert [s["title"] for s in body["sfx"]] == ["door"]
    assert body["layers"] == []
    assert "uri" not in body["sfx"][0]


async def test_play(client):
    response = await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "ambience", "id": "music/ambience/rain.ogg", "volume": 0.5},
        headers=AUTH,
    )
    assert response.status == 200
    assert client.music.calls == [("play", "music/ambience/rain.ogg", "ambience", 0.5, None)]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "ambience", "id": "music/sfx/door.ogg"},
        {"kind": "sfx", "id": "music/tavern.ogg"},
        {"kind": "sfx", "id": "outbox/s1/READY"},
    ],
)
async def test_ids_outside_the_folder_are_404(client, body):
    response = await client.post("/api/v1/soundboard/play", json=body, headers=AUTH)
    assert response.status == 404
    assert client.music.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "music", "id": "music/sfx/door.ogg"},
        {"kind": "sfx"},
        {"kind": "sfx", "id": "music/sfx/door.ogg", "volume": 5},
        {"kind": "sfx", "id": "music/sfx/door.ogg", "volume": True},
        {"kind": "sfx", "id": "music/sfx/door.ogg", "channel_id": "12abc"},
    ],
)
async def test_bad_bodies_are_400(client, body):
    assert (await client.post("/api/v1/soundboard/play", json=body, headers=AUTH)).status == 400


async def test_player_errors_are_409(client):
    client.music.error = MusicError("At most 3 ambience sounds at once.")
    response = await client.post(
        "/api/v1/soundboard/play", json={"kind": "sfx", "id": "music/sfx/door.ogg"}, headers=AUTH
    )
    assert response.status == 409


async def test_stop_variants(client):
    await client.post("/api/v1/soundboard/stop", json={"layer_id": "0a1b2c3d"}, headers=AUTH)
    await client.post("/api/v1/soundboard/stop", json={"kind": "sfx"}, headers=AUTH)
    await client.post("/api/v1/soundboard/stop", json={}, headers=AUTH)
    assert client.music.calls == [
        ("stop_layer", "0a1b2c3d"),
        ("stop_layers", "sfx"),
        ("stop_layers", None),
    ]
    bad = await client.post("/api/v1/soundboard/stop", json={"layer_id": "../x"}, headers=AUTH)
    assert bad.status == 400


async def test_volume(client):
    response = await client.post(
        "/api/v1/soundboard/volume", json={"layer_id": "0a1b2c3d", "volume": 1.2}, headers=AUTH
    )
    assert response.status == 200
    assert client.music.calls == [("volume", "0a1b2c3d", 1.2)]
    missing = await client.post(
        "/api/v1/soundboard/volume", json={"layer_id": "0a1b2c3d"}, headers=AUTH
    )
    assert missing.status == 400


async def test_needs_the_token(client):
    assert (await client.get("/api/v1/soundboard")).status == 401
