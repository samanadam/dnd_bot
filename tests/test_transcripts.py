"""Transcripts for the portal: parsing, paging, and keeping user ids out."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dnd_bot import paths
from dnd_bot.api.server import build_app
from dnd_bot.config import Config
from dnd_bot.transcripts import TranscriptMissing, TranscriptReader, from_json, from_markdown

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}

JSON = {
    "session_id": "session-1",
    "session_name": "Test Session",
    "timezone": "Europe/Istanbul",
    "duration_seconds": 3723.5,
    "language": "tr",
    "model": "large-v3",
    "speakers": ["Aria", "DM"],
    "word_count": 6,
    "warnings": ["Track for /data/sessions/x/audio/10.opus was empty"],
    "segments": [
        {
            "speaker": "DM",
            "user_id": "100000000000000042",
            "start": 1.25,
            "end": 3.0,
            "text": "Zar at.",
            "start_local": "2026-05-01T21:00:01+03:00",
        },
        {
            "speaker": "Aria",
            "user_id": "400000000000000001",
            "start": 3725,
            "end": 3726,
            "text": "On yedi!",
        },
        {"speaker": "Aria", "user_id": "1", "start": 3727, "end": 3728, "text": "   "},
    ],
}

MARKDOWN = """# Test Session

- **Date:** 2026-05-01 21:00:00 +03
- **Duration:** 1:02:03
- **Speakers:** 2 (Aria, DM)
- **Words:** 5

## Transcript

[21:00:01] DM: Zar at.
[22:02:05] Aria: On yedi!
continued line
"""


def test_json_drops_user_ids_and_empty_segments():
    parsed = from_json(JSON)
    assert len(parsed.segments) == 2
    assert all("user_id" not in segment for segment in parsed.segments)
    assert "100000000000000042" not in json.dumps(parsed.segments)
    assert parsed.segments[0] == {
        "speaker": "DM",
        "start": 1.25,
        "end": 3.0,
        "clock": "00:00:01",
        "text": "Zar at.",
    }
    assert parsed.segments[1]["clock"] == "01:02:05"
    assert parsed.meta["speakers"] == ["Aria", "DM"]
    assert parsed.meta["word_count"] == 4


def test_markdown_fallback():
    parsed = from_markdown(MARKDOWN)
    assert [s["speaker"] for s in parsed.segments] == ["DM", "Aria"]
    assert parsed.segments[1]["text"] == "On yedi! continued line"
    assert parsed.segments[0]["clock"] == "21:00:01"
    assert parsed.segments[0]["start"] is None


def test_reader_prefers_json_and_falls_back(tmp_path):
    reader = TranscriptReader(tmp_path)
    with pytest.raises(TranscriptMissing):
        reader.read("session-1")
    md = paths.transcript_md_path(tmp_path, "session-1")
    md.parent.mkdir(parents=True)
    md.write_text(MARKDOWN, encoding="utf-8")
    assert reader.read("session-1").segments[0]["start"] is None
    paths.transcript_json_path(tmp_path, "session-1").write_text(json.dumps(JSON), encoding="utf-8")
    assert reader.read("session-1").segments[0]["start"] == 1.25


def test_broken_json_falls_back_to_markdown(tmp_path):
    reader = TranscriptReader(tmp_path)
    md = paths.transcript_md_path(tmp_path, "s")
    md.parent.mkdir(parents=True)
    md.write_text(MARKDOWN, encoding="utf-8")
    paths.transcript_json_path(tmp_path, "s").write_text("{not json", encoding="utf-8")
    assert len(reader.read("s").segments) == 2


@pytest.fixture
async def client(config: Config, session_row):
    cfg = replace(config, api_enabled=True, api_token=TOKEN)
    cfg.ensure_dirs()

    async def get_session(session_id):
        return session_row if session_id == session_row["id"] else None

    bot = SimpleNamespace(
        config=cfg,
        db=SimpleNamespace(pending_count=lambda: 0, get_session=get_session),
        manager=SimpleNamespace(active={}, sessions_in_guild=lambda gid: []),
        store=None,
        music=None,
        is_ready=lambda: True,
    )
    async with TestClient(TestServer(build_app(bot))) as test_client:
        test_client.cfg = cfg
        yield test_client


def write_json(cfg, session_id="session-1", payload=JSON):
    path = paths.transcript_json_path(cfg.sessions_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


async def test_transcript_route(client):
    write_json(client.cfg)
    response = await client.get("/api/v1/sessions/session-1/transcript", headers=AUTH)
    assert response.status == 200
    body = await response.json()
    assert body["total"] == 2
    assert body["session"]["name"] == "Test Session"
    assert body["session"]["language"] == "tr"
    assert "/data/" not in json.dumps(body)
    assert "user_id" not in json.dumps(body)
    assert "100000000000000042" not in json.dumps(body)


async def test_paging(client):
    write_json(client.cfg)
    body = await (
        await client.get("/api/v1/sessions/session-1/transcript?offset=1&limit=1", headers=AUTH)
    ).json()
    assert [s["text"] for s in body["segments"]] == ["On yedi!"]
    assert (body["offset"], body["limit"], body["total"]) == (1, 1, 2)


@pytest.mark.parametrize("query", ["limit=0", "limit=1001", "offset=-1", "limit=abc"])
async def test_bad_paging_is_400(client, query):
    write_json(client.cfg)
    response = await client.get(f"/api/v1/sessions/session-1/transcript?{query}", headers=AUTH)
    assert response.status == 400


async def test_missing_session_and_transcript(client):
    assert (await client.get("/api/v1/sessions/nope/transcript", headers=AUTH)).status == 404
    response = await client.get("/api/v1/sessions/session-1/transcript", headers=AUTH)
    assert response.status == 404
    assert (await response.json())["error"]["code"] == "no_transcript"


async def test_odd_session_ids_are_404(client):
    response = await client.get("/api/v1/sessions/a%2E%2E/transcript", headers=AUTH)
    assert response.status == 404


async def test_needs_the_token(client):
    assert (await client.get("/api/v1/sessions/session-1/transcript")).status == 401
