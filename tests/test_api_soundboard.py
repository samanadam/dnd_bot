"""Soundboard routes: every id is checked against its folder before playing."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.music import MusicError, Track
from dnd_bot.ytdlp import TrackResolutionError

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


WATCH = "https://www.youtube.com/watch?v=abcdefghijk"


class FakeYouTube:
    name = "youtube"
    enabled = True

    def __init__(self):
        self.fetched = []
        self.error = None

    async def fetch_layer(self, track_id, kind):
        self.fetched.append((track_id, kind))
        if self.error:
            raise self.error
        return Track(
            track_id, "thunder", self.name, f"/cache/music/{self.name}/x_abcdefghijk.m4a", 12.0
        )


class FakeSoundCloud(FakeYouTube):
    name = "soundcloud"


CLOUD = "https://soundcloud.com/wolfbravery/tavern-ambience"


class FakeMusic:
    def __init__(self):
        self.sources = {
            "r2": FakeSource(),
            "youtube": FakeYouTube(),
            "soundcloud": FakeSoundCloud(),
        }
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


# -- YouTube sounds ----------------------------------------------------------


async def test_a_youtube_sound_is_fetched_then_played(client):
    response = await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "sfx", "source": "youtube", "id": WATCH, "volume": 0.7},
        headers=AUTH,
    )
    assert response.status == 200
    assert client.music.sources["youtube"].fetched == [(WATCH, "sfx")]
    assert client.music.calls == [("play", WATCH, "sfx", 0.7, None)]


async def test_youtube_sounds_skip_the_bucket_listing(client):
    # Not in any R2 folder, and still fine: the resolver is the authority.
    client.music.sources["r2"].folders = {"ambience": [], "sfx": []}
    response = await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "ambience", "source": "youtube", "id": WATCH},
        headers=AUTH,
    )
    assert response.status == 200


async def test_the_default_source_is_still_the_bucket(client):
    await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "sfx", "id": "music/sfx/door.ogg"},
        headers=AUTH,
    )
    assert client.music.sources["youtube"].fetched == []


@pytest.mark.parametrize("source", ["spotify", "", None, 5, ["youtube"], "YouTube"])
async def test_an_unknown_source_is_400(client, source):
    response = await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "sfx", "source": source, "id": WATCH},
        headers=AUTH,
    )
    assert response.status == 400
    assert client.music.calls == []


async def test_a_refused_link_is_a_502_and_nothing_plays(client):
    client.music.sources["youtube"].error = TrackResolutionError("That link is a playlist.")
    response = await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "sfx", "source": "youtube", "id": "https://www.youtube.com/playlist?list=x"},
        headers=AUTH,
    )
    assert response.status == 502
    assert client.music.calls == []


async def test_youtube_being_off_is_a_503(client):
    del client.music.sources["youtube"]
    response = await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "sfx", "source": "youtube", "id": WATCH},
        headers=AUTH,
    )
    assert response.status == 503


async def test_prepare_downloads_without_playing(client):
    response = await client.post(
        "/api/v1/soundboard/prepare", json={"kind": "sfx", "id": WATCH}, headers=AUTH
    )
    assert response.status == 200
    body = await response.json()
    assert body == {
        "id": WATCH,
        "title": "thunder",
        "source": "youtube",
        "duration_seconds": 12.0,
    }
    assert "uri" not in body and "cache" not in str(body)
    assert client.music.calls == []
    assert client.music.sources["youtube"].fetched == [(WATCH, "sfx")]


@pytest.mark.parametrize(
    "body",
    [{"kind": "music", "id": WATCH}, {"kind": "sfx"}, {"kind": "sfx", "id": ""}, {"id": WATCH}],
)
async def test_prepare_rejects_bad_bodies(client, body):
    response = await client.post("/api/v1/soundboard/prepare", json=body, headers=AUTH)
    assert response.status == 400
    assert client.music.sources["youtube"].fetched == []


async def test_prepare_reports_a_failed_download(client):
    client.music.sources["youtube"].error = TrackResolutionError("That video is private.")
    response = await client.post(
        "/api/v1/soundboard/prepare", json={"kind": "sfx", "id": WATCH}, headers=AUTH
    )
    assert response.status == 502
    assert "private" in (await response.json())["error"]["message"]


async def test_prepare_needs_the_token(client):
    response = await client.post("/api/v1/soundboard/prepare", json={"kind": "sfx", "id": WATCH})
    assert response.status == 401


# -- SoundCloud sounds -------------------------------------------------------


async def test_a_soundcloud_sound_is_fetched_from_its_own_source_then_played(client):
    response = await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "ambience", "source": "soundcloud", "id": CLOUD, "volume": 0.4},
        headers=AUTH,
    )
    assert response.status == 200
    assert client.music.sources["soundcloud"].fetched == [(CLOUD, "ambience")]
    assert client.music.sources["youtube"].fetched == []
    assert client.music.calls == [("play", CLOUD, "ambience", 0.4, None)]


async def test_prepare_takes_a_soundcloud_source(client):
    response = await client.post(
        "/api/v1/soundboard/prepare",
        json={"kind": "sfx", "source": "soundcloud", "id": CLOUD},
        headers=AUTH,
    )
    assert response.status == 200
    body = await response.json()
    assert body["source"] == "soundcloud"
    assert "uri" not in body and "cache" not in str(body)
    assert client.music.sources["soundcloud"].fetched == [(CLOUD, "sfx")]
    assert client.music.sources["youtube"].fetched == []


@pytest.mark.parametrize("source", ["r2", "spotify", 5, None])
async def test_prepare_only_takes_a_downloaded_source(client, source):
    response = await client.post(
        "/api/v1/soundboard/prepare",
        json={"kind": "sfx", "source": source, "id": CLOUD},
        headers=AUTH,
    )
    assert response.status == 400


async def test_soundcloud_being_off_is_a_503(client):
    del client.music.sources["soundcloud"]
    response = await client.post(
        "/api/v1/soundboard/play",
        json={"kind": "sfx", "source": "soundcloud", "id": CLOUD},
        headers=AUTH,
    )
    assert response.status == 503
