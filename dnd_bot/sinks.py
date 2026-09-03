"""Incremental, crash-resilient recording sink.

py-cord's stock sinks buffer the whole session in memory and only write at stop.
A four-hour D&D session with six speakers would be gigabytes of RSS on a box
that has 8 GB total, and a crash would lose everything. This sink instead opens
one file handle per speaker and appends every packet as it arrives, flushing and
fsync-ing periodically so at most a few seconds per speaker can be lost.

Raw PCM goes to `<user_id>.pcm`; a WAV/MP3 container is only produced at
finalize time (see `audio.finalize_capture`).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from discord.sinks import Sink

from .audio import BYTES_PER_SECOND

log = logging.getLogger(__name__)


class DiskSink(Sink):
    """Writes each speaker's PCM straight to disk as packets arrive.

    Note: py-cord invokes `write()` from the voice receive thread, so callbacks
    handed in here must be thread-safe. The recorder marshals them back onto the
    event loop with `asyncio.run_coroutine_threadsafe`.
    """

    encoding = "pcm"

    # py-cord 2.8 ships a rewritten receive path whose SinkEventRouter reads
    # these off every sink, but the matching Sink base class did not land with
    # it - discord.sinks.Sink still has neither, so start_recording() dies with
    # AttributeError before a single packet arrives. Audio itself does not go
    # through the listener system (PacketRouter calls sink.write directly), so
    # declaring an empty set of listeners is enough to get recording working.
    # Remove once the sink rewrite lands upstream (Pycord issue #3139).
    __sink_listeners__: tuple[tuple[str, str], ...] = ()

    def walk_children(self) -> tuple[()]:
        return ()

    def is_opus(self) -> bool:
        """False, so py-cord decodes Opus to PCM before handing packets over."""
        return False

    def __init__(
        self,
        raw_dir: Path,
        *,
        flush_interval: float = 5.0,
        on_new_speaker: Callable[[str, float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        base_offset: float = 0.0,
        known_offsets: dict[str, float] | None = None,
    ) -> None:
        super().__init__()
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.flush_interval = flush_interval
        self.on_new_speaker = on_new_speaker
        self._clock = clock
        # After a voice reconnect a fresh sink resumes an existing session, so
        # offsets must stay relative to the session start, not to this sink.
        self._base_offset = base_offset
        self._known_offsets = dict(known_offsets or {})
        self._started_at = clock()
        self._files: dict[str, BinaryIO] = {}
        self._offsets: dict[str, float] = {}
        self._bytes: dict[str, int] = {}
        self._last_flush: dict[str, float] = {}
        self.finished = False

    # -- state -------------------------------------------------------------

    @property
    def offsets(self) -> dict[str, float]:
        """Seconds between session start and each speaker's first packet."""
        return dict(self._offsets)

    @property
    def speakers(self) -> list[str]:
        """Everyone heard this session - still accurate after cleanup()."""
        return list(self._offsets)

    def bytes_written(self, user_id: str) -> int:
        return self._bytes.get(str(user_id), 0)

    def duration_seconds(self, user_id: str) -> float:
        return self.bytes_written(user_id) / BYTES_PER_SECOND

    def path_for(self, user_id: str) -> Path:
        return self.raw_dir / f"{user_id}.pcm"

    # -- recording ---------------------------------------------------------

    def write(self, data, user) -> None:  # noqa: ANN001 - py-cord signature
        """Append one speaker's PCM.

        py-cord 2.8 hands in a VoiceData carrying decoded PCM plus the speaker;
        older versions passed raw bytes and a user. Accept both.
        """
        if self.finished:
            return
        pcm = getattr(data, "pcm", data)
        user = getattr(data, "source", None) or user
        if not pcm or user is None:
            # A packet Discord could not attribute to anyone is unusable: it
            # cannot be labelled, so it would only pollute an unnamed file.
            return
        user_id = str(getattr(user, "id", user))
        handle = self._files.get(user_id)
        now = self._clock()

        if handle is None:
            offset = self._known_offsets.get(
                user_id, self._base_offset + max(0.0, now - self._started_at)
            )
            path = self.path_for(user_id)
            # Append mode so a /session recover after a partial write is additive.
            handle = path.open("ab")
            self._files[user_id] = handle
            self._offsets[user_id] = offset
            self._bytes[user_id] = 0
            self._last_flush[user_id] = now
            log.info("New speaker %s in %s at +%.1fs", user_id, self.raw_dir, offset)
            if self.on_new_speaker is not None:
                try:
                    self.on_new_speaker(user_id, offset)
                except Exception:  # noqa: BLE001 - never kill the receive thread
                    log.exception("on_new_speaker callback failed for %s", user_id)

        try:
            handle.write(pcm)
        except OSError:
            log.exception("Failed writing audio for user %s", user_id)
            return
        self._bytes[user_id] = self._bytes.get(user_id, 0) + len(pcm)

        if now - self._last_flush.get(user_id, 0.0) >= self.flush_interval:
            self._flush_one(user_id)
            self._last_flush[user_id] = now

    def _flush_one(self, user_id: str) -> None:
        handle = self._files.get(user_id)
        if handle is None or handle.closed:
            return
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except OSError:
            log.exception("Failed flushing audio for user %s", user_id)

    def flush_all(self) -> None:
        for user_id in list(self._files):
            self._flush_one(user_id)

    def cleanup(self) -> None:
        """Close every handle. Safe to call more than once."""
        self.finished = True
        for user_id, handle in list(self._files.items()):
            self._flush_one(user_id)
            try:
                handle.close()
            except OSError:
                log.exception("Failed closing audio file for user %s", user_id)
        self._files.clear()

    # py-cord calls this on the base class after recording stops; we already
    # write finished files ourselves, so there is nothing to convert here.
    def format_audio(self, audio) -> None:  # noqa: ANN001 - py-cord signature
        return None
