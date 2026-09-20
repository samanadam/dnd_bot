"""Soundboard: ambience loops and one-shot effects mixed over the music.

Sounds live in the music bucket under `ambience/` and `sfx/`. Every id is
checked against that folder's live listing before anything is downloaded, the
same allowlist rule the music library uses.
"""

from __future__ import annotations

import logging
import re

from aiohttp import web

from ..music import LAYER_KINDS
from .keys import CONFIG
from .middleware import ApiError, read_json
from .routes_music import _music, _source

log = logging.getLogger(__name__)

routes = web.RouteTableDef()

LAYER_ID = re.compile(r"^[0-9a-f]{8}$")
# Sources whose sounds are downloaded once into the music cache and played from disk.
DOWNLOADED_SOURCES = ("youtube", "soundcloud")


def _kind(value: object) -> str:
    if value not in LAYER_KINDS:
        raise ApiError(400, "bad_request", "kind must be ambience or sfx.")
    return str(value)


def _volume(value: object, default: float = 1.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ApiError(400, "bad_request", "volume must be a number between 0 and 2.")
    if not 0.0 <= float(value) <= 2.0:
        raise ApiError(400, "bad_request", "volume must be a number between 0 and 2.")
    return float(value)


def _state(request: web.Request) -> web.Response:
    return web.json_response(_music(request).soundboard_state(request.app[CONFIG].guild_id))


@routes.get("/api/v1/soundboard")
async def library(request: web.Request) -> web.Response:
    """Every sound, grouped by folder, plus what is playing now."""
    source = _source(request, "r2")
    music = _music(request)
    body = {
        "ambience": [t.to_dict() for t in await source.browse_folder("ambience")],
        "sfx": [t.to_dict() for t in await source.browse_folder("sfx")],
        **music.soundboard_state(request.app[CONFIG].guild_id),
    }
    return web.json_response(body)


def _track_id(payload: dict) -> str:
    track_id = payload.get("id")
    if not isinstance(track_id, str) or not track_id or len(track_id) > 512:
        raise ApiError(400, "bad_request", "id is required.")
    return track_id


def _source_name(payload: dict) -> str:
    name = payload.get("source", "r2")
    if name not in ("r2", *DOWNLOADED_SOURCES):
        raise ApiError(400, "bad_request", "source must be r2, youtube or soundcloud.")
    return str(name)


async def _resolve_layer(request: web.Request, source_name: str, kind: str, track_id: str):
    """A playable local file for one sound.

    A bucket sound must be in its folder's live listing. A YouTube sound is
    downloaded once into the music cache by the resolver, which validates the
    link and every limit itself.
    """
    source = _source(request, source_name)
    if source_name in DOWNLOADED_SOURCES:
        return await source.fetch_layer(track_id, kind)
    allowed = {track.id for track in await source.browse_folder(kind)}
    if track_id not in allowed:
        raise ApiError(404, "not_found", f"No such {kind} sound.")
    return await source.resolve(track_id)


@routes.post("/api/v1/soundboard/prepare")
async def prepare(request: web.Request) -> web.Response:
    """Download a YouTube or SoundCloud sound ahead of time so it starts at once later."""
    payload = await read_json(request)
    kind = _kind(payload.get("kind"))
    source_name = _source_name({"source": payload.get("source", "youtube")})
    if source_name not in DOWNLOADED_SOURCES:
        raise ApiError(400, "bad_request", "Only YouTube and SoundCloud sounds are prepared.")
    track = await _resolve_layer(request, source_name, kind, _track_id(payload))
    return web.json_response(track.to_dict())


@routes.post("/api/v1/soundboard/play")
async def play(request: web.Request) -> web.Response:
    payload = await read_json(request)
    kind = _kind(payload.get("kind"))
    track_id = _track_id(payload)
    source_name = _source_name(payload)
    volume = _volume(payload.get("volume"))
    channel_id = payload.get("channel_id")
    if channel_id is not None and not (isinstance(channel_id, str) and channel_id.isdigit()):
        raise ApiError(400, "bad_request", "channel_id must be a numeric id.")

    track = await _resolve_layer(request, source_name, kind, track_id)

    await _music(request).play_layer(
        request.app[CONFIG].guild_id,
        track,
        kind=kind,
        volume=volume,
        channel_id=int(channel_id) if channel_id else None,
    )
    return _state(request)


@routes.post("/api/v1/soundboard/stop")
async def stop(request: web.Request) -> web.Response:
    """Stop one layer by id, every layer of a kind, or everything."""
    payload = await read_json(request)
    music = _music(request)
    guild_id = request.app[CONFIG].guild_id
    layer_id = payload.get("layer_id")
    if layer_id is not None:
        if not isinstance(layer_id, str) or not LAYER_ID.match(layer_id):
            raise ApiError(400, "bad_request", "layer_id is not valid.")
        await music.stop_layer(guild_id, layer_id)
    else:
        kind = payload.get("kind")
        await music.stop_layers(guild_id, _kind(kind) if kind is not None else None)
    return _state(request)


@routes.post("/api/v1/soundboard/volume")
async def volume(request: web.Request) -> web.Response:
    payload = await read_json(request)
    layer_id = payload.get("layer_id")
    if not isinstance(layer_id, str) or not LAYER_ID.match(layer_id):
        raise ApiError(400, "bad_request", "layer_id is not valid.")
    if payload.get("volume") is None:
        raise ApiError(400, "bad_request", "volume is required.")
    await _music(request).set_layer_volume(
        request.app[CONFIG].guild_id, layer_id, _volume(payload.get("volume"))
    )
    return _state(request)
