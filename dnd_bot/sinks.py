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
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from discord.sinks import Sink

from .audio import BYTES_PER_SECOND
from .gaps import append_gap, gaps_path, silence_bytes_for, total_gap_bytes

log = logging.getLogger(__name__)


# A pause is told from a network stall by what follows it. After real silence
# the speaker's packets arrive at the pace they were spoken; after a stall the
# backlog arrives all at once. This many packets, spanning less than the burst
# window, mean the audio was only late - it already sits in the right place.
STALL_PROBE_PACKETS = 3
STALL_BURST_SECONDS = 0.03


@dataclass
class _HeldGap:
    """A pause seen but not yet written, waiting to learn if it was a stall."""

    position: int
    silence_bytes: int
    first_arrival: float
    packets_since: int = 0


class DiskSink(Sink):
    """Writes each speaker's PCM straight to disk as packets arrive.

    Note: py-cord invokes `write()` from the voice receive thread, so callbacks
    handed in here must be thread-safe. The recorder marshals them back onto the
    event loop with `asyncio.run_coroutine_threadsafe`.
    """

    encoding = "pcm"

    # The receive path's SinkEventRouter reads these off every sink. Released
    # py-cord 2.8.x has the router but not the matching Sink base class, so
    # start_recording() died with AttributeError before a packet arrived; the
    # pinned voice-receive branch does define both. Declaring them here keeps
    # the sink working on either side of that line. Audio itself never goes
    # through the listener system - PacketRouter calls sink.write directly -
    # so an empty set of listeners is correct, not just a stopgap.
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
        ignore_user_ids: set[str] | None = None,
        gap_threshold: float = 0.1,
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
        # Belt and braces for music playback: Discord does not send a bot its
        # own transmission back, so this should never fire - but a stray track
        # file named after the bot would be a confusing way to find that out.
        self._ignore_user_ids = set(ignore_user_ids or ())
        self._started_at = clock()
        self._files: dict[str, BinaryIO] = {}
        self._offsets: dict[str, float] = {}
        self._bytes: dict[str, int] = {}
        self._last_flush: dict[str, float] = {}
        # Pauses shorter than this are packet jitter, not silence.
        self._gap_threshold = gap_threshold
        self._gap_files: dict[str, BinaryIO] = {}
        self._pcm_position: dict[str, int] = {}
        self._timeline_end: dict[str, float] = {}
        self._held: dict[str, _HeldGap] = {}
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
        if user_id in self._ignore_user_ids or getattr(user, "bot", False):
            return
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
            # What is already on disk (a resumed sink, a recover) is part of this
            # speaker's timeline: the pcm plus the pauses recorded in it.
            existing = path.stat().st_size
            self._pcm_position[user_id] = existing
            self._timeline_end[user_id] = (
                offset + (existing + total_gap_bytes(gaps_path(path))) / BYTES_PER_SECOND
            )
            log.info("New speaker %s in %s at +%.1fs", user_id, self.raw_dir, offset)
            if self.on_new_speaker is not None:
                try:
                    self.on_new_speaker(user_id, offset)
                except Exception:  # noqa: BLE001 - never kill the receive thread
                    log.exception("on_new_speaker callback failed for %s", user_id)

        gap = self._detect_gap(user_id, now)
        position = self._pcm_position[user_id]
        try:
            handle.write(pcm)
        except OSError:
            log.exception("Failed writing audio for user %s", user_id)
            return
        self._bytes[user_id] = self._bytes.get(user_id, 0) + len(pcm)
        self._pcm_position[user_id] += len(pcm)
        self._timeline_end[user_id] += len(pcm) / BYTES_PER_SECOND
        # Only once the audio is safely written: a pause that precedes nothing
        # would otherwise be recorded against audio that never reached disk.
        if gap is not None:
            self._held[user_id] = _HeldGap(position, gap, now)
        else:
            self._settle_held(user_id, now)

        if now - self._last_flush.get(user_id, 0.0) >= self.flush_interval:
            self._flush_one(user_id)
            self._last_flush[user_id] = now

    def _detect_gap(self, user_id: str, now: float) -> int | None:
        """Silence (in bytes) this speaker's timeline has fallen behind by.

        Discord sends nothing while someone is quiet, so the capture would
        glue their bursts together. Anchoring the speaker's timeline to the
        session clock (not to the previous packet) means jitter never adds
        up, and a burst that arrives early simply pads nothing until the
        clock catches up. A pause already being weighed is not detected twice.
        """
        if user_id in self._held:
            return None
        session_now = self._base_offset + max(0.0, now - self._started_at)
        behind = session_now - self._timeline_end[user_id]
        if behind < self._gap_threshold:
            return None
        return silence_bytes_for(behind) or None

    def _settle_held(self, user_id: str, now: float) -> None:
        """Decide a waiting pause once enough packets have followed it."""
        held = self._held.get(user_id)
        if held is None:
            return
        held.packets_since += 1
        if held.packets_since < STALL_PROBE_PACKETS:
            return
        del self._held[user_id]
        if now - held.first_arrival < STALL_BURST_SECONDS:
            log.info(
                "Treating a %.1fs delay for %s as a stall, not a pause",
                self._seconds(held),
                user_id,
            )
            return
        self._commit(user_id, held)

    def _commit(self, user_id: str, held: _HeldGap) -> None:
        handle = self._gap_files.get(user_id)
        try:
            if handle is None:
                handle = gaps_path(self.path_for(user_id)).open("ab")
                self._gap_files[user_id] = handle
            append_gap(handle, held.position, held.silence_bytes)
        except OSError:
            log.exception("Failed recording a pause for user %s", user_id)
            return
        self._timeline_end[user_id] += self._seconds(held)

    @staticmethod
    def _seconds(held: _HeldGap) -> float:
        return held.silence_bytes / BYTES_PER_SECOND

    def _flush_one(self, user_id: str) -> None:
        handle = self._files.get(user_id)
        if handle is None or handle.closed:
            return
        try:
            handle.flush()
            os.fsync(handle.fileno())
            gap_handle = self._gap_files.get(user_id)
            if gap_handle is not None and not gap_handle.closed:
                gap_handle.flush()
                os.fsync(gap_handle.fileno())
        except OSError:
            log.exception("Failed flushing audio for user %s", user_id)

    def flush_all(self) -> None:
        for user_id in list(self._files):
            self._flush_one(user_id)

    def cleanup(self) -> None:
        """Close every handle. Safe to call more than once."""
        self.finished = True
        # A pause still being weighed when the recording ends was a pause: a
        # stall's backlog would have arrived by now.
        for user_id, held in list(self._held.items()):
            self._commit(user_id, held)
        self._held.clear()
        for user_id, handle in list(self._files.items()):
            self._flush_one(user_id)
            try:
                handle.close()
            except OSError:
                log.exception("Failed closing audio file for user %s", user_id)
        self._files.clear()
        for user_id, handle in list(self._gap_files.items()):
            try:
                handle.close()
            except OSError:
                log.exception("Failed closing gap file for user %s", user_id)
        self._gap_files.clear()

    # py-cord calls this on the base class after recording stops; we already
    # write finished files ourselves, so there is nothing to convert here.
    def format_audio(self, audio) -> None:  # noqa: ANN001 - py-cord signature
        return None
