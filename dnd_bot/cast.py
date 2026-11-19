"""Live casts: a browser tab's audio, played into the voice channel.

The portal records a tab with MediaRecorder (webm/opus) and sends the chunks to
a WebSocket here. ffmpeg decodes them from stdin to 48 kHz stereo PCM, and
`CastSource` hands that to the voice connection one 20 ms frame at a time, so a
cast is just another music source as far as the mixer is concerned.

Two threads touch a source: the event loop feeds it and py-cord's player thread
reads it every 20 ms. `read()` therefore never blocks and never returns a short
frame (a short frame ends a track), only a full frame or silence.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import discord
from aiohttp import WSMsgType

from .mixer import FRAME_BYTES, SILENCE

log = logging.getLogger(__name__)

TICKET_SECONDS = 30.0
FIRST_MESSAGE_SECONDS = 5.0
IDLE_SECONDS = 10.0
MAX_CAST_SECONDS = 6 * 3600.0
# 16 KB/s is what the browser sends at 128 kbit; this only stops a flood.
MAX_BYTES_PER_SECOND = 64 * 1024
RATE_WINDOW_SECONDS = 5.0
MAX_MESSAGE_BYTES = 256 * 1024
MAX_NAME_CHARS = 40
# 16-bit stereo: the smallest unit audio can be cut on.
SAMPLE_BYTES = 4

# Aim to hold 300 ms before playing, and never hold more than 2 s: past that the
# oldest audio is dropped so the delay cannot grow.
TARGET_FRAMES = 20
# After the buffer ran dry, restart at half the cushion: a shorter gap than a full
# refill, and the buffer keeps growing again while it plays.
RESUME_FRAMES = 10
CAP_FRAMES = 100

# Input is whitelisted to the pipe and the container to webm, so the bytes a
# signed-in user sends can never name a file or a URL for ffmpeg to open.
FFMPEG_ARGS = (
    "-hide_banner",
    "-loglevel",
    "error",
    "-fflags",
    "nobuffer",
    "-flags",
    "low_delay",
    "-probesize",
    "32768",
    "-analyzeduration",
    "0",
    "-protocol_whitelist",
    "pipe",
    "-f",
    "webm",
    "-i",
    "pipe:0",
    "-vn",
    "-f",
    "s16le",
    "-ar",
    "48000",
    "-ac",
    "2",
    "pipe:1",
)


class CastError(RuntimeError):
    """User-facing cast failure. Becomes a 409 like MusicError."""


class CastSource(discord.AudioSource):
    """A thread-safe jitter buffer that is a voice source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._primed = False
        self._started = False
        self._ended = False
        self.closed = False
        # Counted for the log line when a cast ends: how often it ran dry or
        # had to shed audio is what tells a slow link from a bad decode.
        self.underruns = 0
        self.shed_events = 0
        self.fed_bytes = 0

    # -- event loop side ---------------------------------------------------

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            if self.closed:
                return
            self._buffer += pcm
            self.fed_bytes += len(pcm)
            cap = CAP_FRAMES * FRAME_BYTES
            if len(self._buffer) > cap:
                # Keep the newest audio. The cut must land on a whole stereo
                # sample (4 bytes): an odd offset turns the rest of the cast
                # into static.
                excess = len(self._buffer) - TARGET_FRAMES * FRAME_BYTES
                del self._buffer[: excess - excess % SAMPLE_BYTES]
                self.shed_events += 1
            need = RESUME_FRAMES if self._started else TARGET_FRAMES
            if len(self._buffer) >= need * FRAME_BYTES:
                self._primed = True

    def finish(self) -> None:
        """No more audio is coming; the track ends once the buffer drains."""
        with self._lock:
            self._ended = True
            self._buffer.clear()

    # -- player thread side ------------------------------------------------

    def read(self) -> bytes:
        with self._lock:
            if self.closed or (self._ended and len(self._buffer) < FRAME_BYTES):
                return b""
            if not self._primed or len(self._buffer) < FRAME_BYTES:
                # Underrun: wait for the buffer to refill instead of stuttering.
                if self._primed:
                    self.underruns += 1
                self._primed = False
                return SILENCE
            self._started = True
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            return frame

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._lock:
            self.closed = True
            self._buffer.clear()


@dataclass
class CastSession:
    id: str
    name: str
    source: CastSource = field(default_factory=CastSource)
    started_at: float = field(default_factory=time.time)
    connected: bool = False
    ended: bool = False
    ws: Any = None
    proc: Any = None
    expiry: asyncio.TimerHandle | None = None


class CastManager:
    """One cast at a time per bot, and the single-use tickets that start it."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._active: CastSession | None = None
        self._tickets: dict[str, tuple[CastSession, float]] = {}

    # -- lifecycle ---------------------------------------------------------

    def create(self, name: str) -> tuple[CastSession, str]:
        """Register a new cast. A cast already running is not touched here: the
        caller plays the new one "now", which halts the old track, and the old
        session then notices its source closed and ends itself. Ending it first
        would report a normal track end and start the next queued track."""
        session = CastSession(id=secrets.token_hex(8), name=name[:MAX_NAME_CHARS])
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (session, self._clock() + TICKET_SECONDS)
        self._active = session
        # A browser that never opens its socket must not hold the slot.
        session.expiry = asyncio.get_running_loop().call_later(
            TICKET_SECONDS + FIRST_MESSAGE_SECONDS, self._expire_unconnected, session
        )
        return session, ticket

    def _expire_unconnected(self, session: CastSession) -> None:
        if not session.connected:
            self.end(session)

    def redeem(self, ticket: str) -> CastSession | None:
        """Single use: the ticket is gone whether or not it was still valid."""
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return None
        session, deadline = entry
        if session.ended or self._clock() > deadline:
            return None
        session.connected = True
        return session

    def end(self, session: CastSession) -> None:
        if session.ended:
            return
        session.ended = True
        if session.expiry is not None:
            session.expiry.cancel()
        for ticket in [t for t, (s, _) in self._tickets.items() if s is session]:
            del self._tickets[ticket]
        if self._active is session:
            self._active = None
        source = session.source
        log.info(
            "Cast %s ended after %.0fs: %d KB decoded, %d underruns, %d overflow trims",
            session.id,
            time.time() - session.started_at,
            source.fed_bytes // 1024,
            source.underruns,
            source.shed_events,
        )
        source.finish()
        proc, session.proc = session.proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        ws = session.ws
        if ws is not None and not ws.closed:
            asyncio.ensure_future(ws.close())

    def stop(self) -> bool:
        if self._active is None:
            return False
        self.end(self._active)
        return True

    def source_for(self, cast_id: str) -> CastSource:
        session = self._active
        if session is None or session.id != cast_id or session.ended:
            raise CastError("That cast has ended.")
        return session.source

    def summary(self) -> dict[str, Any] | None:
        session = self._active
        if session is None or session.ended:
            return None
        return {"by_name": session.name, "started_at": session.started_at}

    # -- the socket --------------------------------------------------------

    async def serve(self, ws, session: CastSession) -> None:
        """Pump one browser's chunks through ffmpeg until anything ends it."""
        session.ws = ws
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            *FFMPEG_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        session.proc = proc
        reader = asyncio.ensure_future(self._decode(proc, session))
        watchdog = asyncio.ensure_future(self._watch(session))
        deadline = self._clock() + MAX_CAST_SECONDS
        window_start, window_bytes = self._clock(), 0
        try:
            while not session.ended and self._clock() < deadline:
                try:
                    message = await ws.receive(timeout=IDLE_SECONDS)
                except TimeoutError:
                    log.info("Cast %s went quiet; closing", session.id)
                    break
                if message.type != WSMsgType.BINARY:
                    break
                now = self._clock()
                if now - window_start > RATE_WINDOW_SECONDS:
                    window_start, window_bytes = now, 0
                window_bytes += len(message.data)
                if window_bytes > MAX_BYTES_PER_SECOND * RATE_WINDOW_SECONDS:
                    log.warning("Cast %s sent too fast; closing", session.id)
                    break
                if proc.stdin is None or proc.stdin.is_closing():
                    break
                proc.stdin.write(message.data)
                await proc.stdin.drain()
        finally:
            reader.cancel()
            watchdog.cancel()
            self.end(session)

    async def _decode(self, proc, session: CastSession) -> None:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(FRAME_BYTES * 5):
            session.source.feed(chunk)

    async def _watch(self, session: CastSession) -> None:
        """The voice side may drop the cast (skip, stop, take over) at any time."""
        while not session.ended:
            if session.source.closed:
                self.end(session)
                return
            await asyncio.sleep(0.5)


def valid_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CastError("name is required.")
    return " ".join(value.split())[:MAX_NAME_CHARS]
