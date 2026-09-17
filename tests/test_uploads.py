"""Uploading to and deleting from the music bucket, through the HTTP API.

The refusals are the point: a file only reaches R2 when its name, size, first
bytes and probed audio all check out, and nothing is ever overwritten.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_r2 import FakeS3

from dnd_bot.api.keys import UPLOADER
from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.r2 import R2Store
from dnd_bot.tracks import R2TrackSource
from dnd_bot.uploads import Probe, Uploader, UploadError, looks_like, object_key, safe_filename

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OGG = b"OggS" + b"\x00" * 60
MP3 = b"ID3" + b"\x00" * 61


# -- names and bytes -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tavern Night.mp3", "Tavern Night.mp3"),
        ("../../etc/passwd.ogg", "passwd.ogg"),
        ("C:\\Music\\Rain.OGG", "Rain.ogg"),
        ("Ejderha Savaşı (final).opus", "Ejderha Savaşı (final).opus"),
        ("bad<>|*?name.flac", "badname.flac"),
        ("  lots    of   space .wav", "lots of space.wav"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "noextension", "script.sh", "....mp3", "evil\x00.mp3", "new\nline.mp3", ".mp3"]
)
def test_unsafe_names_are_refused(raw):
    with pytest.raises(UploadError):
        safe_filename(raw)


def test_object_key_is_built_under_the_folder():
    assert object_key("music", "music", "a.mp3") == "music/a.mp3"
    assert object_key("music", "ambience", "rain.ogg") == "music/ambience/rain.ogg"
    assert object_key("music", "sfx", "../x.ogg") == "music/sfx/x.ogg"
    with pytest.raises(UploadError):
        object_key("music", "../outbox", "a.mp3")


def test_magic_bytes():
    assert looks_like(OGG, ".ogg") and looks_like(OGG, ".opus")
    assert looks_like(MP3, ".mp3")
    assert looks_like(b"\xff\xfb\x90\x00", ".mp3")
    assert looks_like(b"fLaC\x00", ".flac")
    assert looks_like(b"RIFF\x00\x00\x00\x00WAVEfmt ", ".wav")
    assert looks_like(b"\x00\x00\x00\x20ftypM4A ", ".m4a")
    assert not looks_like(b"<?php echo 1;", ".mp3")
    assert not looks_like(OGG, ".wav")


# -- the API -------------------------------------------------------------------


class Prober:
    def __init__(self, probe=Probe(True, 120.0)):
        self.probe = probe
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return self.probe


@pytest.fixture
def s3():
    client = FakeS3()
    client.objects["music/existing.mp3"] = MP3
    client.objects["outbox/s1/READY"] = b""
    return client


@pytest.fixture
async def client(config: Config, s3):
    cfg = replace(
        config, api_enabled=True, api_token=TOKEN, music_enabled=True, music_upload_max_mb=1
    )
    cfg.ensure_dirs()
    store = R2Store(s3, "bucket")
    source = R2TrackSource(store, cfg, prober=lambda _p: None)
    music = SimpleNamespace(sources={"r2": source})
    bot = SimpleNamespace(
        config=cfg,
        db=SimpleNamespace(pending_count=lambda: 0),
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=store,
        music=music,
        is_ready=lambda: True,
    )
    app = build_app(bot)
    prober = Prober()
    app[UPLOADER] = Uploader(cfg, store, source, prober=prober)
    async with TestClient(TestServer(app)) as test_client:
        test_client.prober = prober
        test_client.cfg = cfg
        yield test_client


async def post_file(client, data, *, folder="music", filename="Tavern.ogg", ctype="audio/ogg"):
    return await client.post(
        "/api/v1/music/upload",
        params={"folder": folder, "filename": filename},
        data=data,
        headers={**AUTH, "Content-Type": ctype},
    )


async def test_upload_lands_in_the_bucket(client, s3):
    response = await post_file(client, OGG)
    assert response.status == 201
    body = await response.json()
    assert body == {
        "id": "music/Tavern.ogg",
        "title": "Tavern",
        "folder": "music",
        "size_bytes": len(OGG),
        "duration_seconds": 120.0,
    }
    assert s3.objects["music/Tavern.ogg"] == OGG
    assert s3.extra_args == {"ContentType": "audio/ogg"}
    # Nothing is left on the bot's disk.
    assert not any((client.cfg.data_dir / ".uploads").iterdir())


async def test_soundboard_folders(client, s3):
    assert (await post_file(client, OGG, folder="ambience", filename="rain.ogg")).status == 201
    assert "music/ambience/rain.ogg" in s3.objects


async def test_upload_needs_the_token(client, s3):
    response = await client.post(
        "/api/v1/music/upload",
        params={"filename": "a.ogg"},
        data=OGG,
        headers={"Content-Type": "audio/ogg"},
    )
    assert response.status == 401
    assert "music/a.ogg" not in s3.objects


async def test_existing_names_are_not_overwritten(client, s3):
    response = await post_file(client, MP3, filename="existing.mp3", ctype="audio/mpeg")
    assert response.status == 409
    assert (await response.json())["error"]["code"] == "already_exists"


async def test_json_or_html_content_types_are_refused(client):
    assert (await post_file(client, OGG, ctype="text/html")).status == 415


async def test_wrong_bytes_for_the_extension_are_refused(client, s3):
    response = await post_file(client, b"#!/bin/sh\nrm -rf /\n" * 4, filename="x.ogg")
    assert response.status == 415
    assert (await response.json())["error"]["code"] == "not_audio"
    assert "music/x.ogg" not in s3.objects


async def test_no_audio_stream_is_refused(client, s3):
    client.prober.probe = Probe(False, None)
    assert (await post_file(client, OGG)).status == 415
    assert "music/Tavern.ogg" not in s3.objects


async def test_long_sound_effects_are_refused(client):
    client.prober.probe = Probe(True, 600.0)
    response = await post_file(client, OGG, folder="sfx", filename="boom.ogg")
    assert response.status == 413
    assert (await response.json())["error"]["code"] == "too_long"


async def test_oversized_declared_length_is_refused_before_reading(client):
    response = await post_file(client, b"OggS" + b"\x00" * 1_000_001)
    assert response.status == 413
    assert client.prober.calls == 0


async def test_bad_folder_and_name(client):
    assert (await post_file(client, OGG, folder="outbox")).status == 400
    assert (await post_file(client, OGG, filename="run.sh")).status == 415


async def test_music_library_hides_soundboard_folders(client, s3):
    s3.objects["music/ambience/rain.ogg"] = OGG
    body = await (await client.get("/api/v1/music/library", headers=AUTH)).json()
    assert [track["id"] for track in body] == ["music/existing.mp3"]


async def test_delete(client, s3):
    response = await client.post(
        "/api/v1/music/delete", json={"id": "music/existing.mp3"}, headers=AUTH
    )
    assert response.status == 200
    assert "music/existing.mp3" not in s3.objects


@pytest.mark.parametrize("key", ["outbox/s1/READY", "music/../outbox/s1/READY", "music/nope.mp3"])
async def test_delete_refuses_anything_but_listed_music(client, s3, key):
    response = await client.post("/api/v1/music/delete", json={"id": key}, headers=AUTH)
    assert response.status == 404
    assert "outbox/s1/READY" in s3.objects
