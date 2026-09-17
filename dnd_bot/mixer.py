"""Layering soundboard audio over the music on one voice connection.

Discord plays one AudioSource per voice client. Ambience and sound effects have
to sound *together* with the music, so while any soundboard layer is live the
voice client plays a `MixerSource` instead, which reads one 20 ms frame from
every layer and adds them.

`read()` runs on py-cord's player thread every 20 ms, so it does no I/O beyond
the sources' own reads, holds its lock only for list bookkeeping, and runs
callbacks (which hop back to the event loop) outside that lock.
"""

from __future__ import annotations

import logging
import threading
import warnings
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import discord

log = logging.getLogger(__name__)

# 20 ms of 48 kHz stereo signed 16-bit PCM: what py-cord reads per frame.
FRAME_BYTES = 3840
SILENCE = bytes(FRAME_BYTES)
# How long the mixer keeps going, silent, after the music track ended and the
# next one has not been handed over yet. 100 frames = 2 s.
MAIN_HANDOVER_FRAMES = 100

try:  # audioop is C and fast; it is deprecated in 3.11-3.12 and gone in 3.13.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop as _audioop
except ImportError:  # pragma: no cover - exercised only on Python 3.13+
    _audioop = None


def _pad(frame: bytes) -> bytes:
    return frame + SILENCE[len(frame) :] if len(frame) < FRAME_BYTES else frame[:FRAME_BYTES]


def add_frames(first: bytes, second: bytes) -> bytes:
    """Sample-wise sum of two PCM frames, saturating instead of wrapping."""
    first, second = _pad(first), _pad(second)
    if _audioop is not None:
        return _audioop.add(first, second, 2)
    left, right = array("h", first), array("h", second)
    out = array("h", (max(-32768, min(32767, a + b)) for a, b in zip(left, right, strict=True)))
    return out.tobytes()


@dataclass
class Layer:
    """One soundboard sound inside the mixer."""

    id: str
    kind: str  # "ambience" loops, "sfx" plays once
    track_id: str
    title: str
    source: Any
    volume: float
    # Opens a fresh source for the next loop. None for one-shots.
    reopen: Callable[[], Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "track_id": self.track_id,
            "title": self.title,
            "volume": self.volume,
        }


class MixerSource(discord.AudioSource):
    """A voice source that is the sum of the music track and soundboard layers."""

    def __init__(
        self,
        main: Any | None = None,
        on_main_end: Callable[[Exception | None], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.main = main
        self._on_main_end = on_main_end
        self.main_paused = False
        self.layers: list[Layer] = []
        self._handover = 0
        self.closed = False

    # -- called from the event loop ----------------------------------------

    def set_main(self, source: Any, on_main_end: Callable[[Exception | None], None]) -> bool:
        """Start a music track inside the running mixer. False once it has ended."""
        with self._lock:
            if self.closed:
                return False
            old = self.main
            self.main, self._on_main_end = source, on_main_end
            self.main_paused = False
            self._handover = 0
        if old is not None and old is not source:
            _cleanup(old)
        return True

    def clear_main(self) -> None:
        """Drop the music without reporting its end (a deliberate stop)."""
        with self._lock:
            old, self.main, self._on_main_end = self.main, None, None
            self.main_paused = False
            self._handover = 0
        if old is not None:
            _cleanup(old)

    def end_handover(self) -> None:
        """No next track is coming; stop holding silence for one."""
        with self._lock:
            self._handover = 0

    def add_layer(self, layer: Layer) -> bool:
        with self._lock:
            if self.closed:
                return False
            self.layers.append(layer)
            return True

    def remove_layers(self, predicate: Callable[[Layer], bool]) -> list[Layer]:
        with self._lock:
            removed = [layer for layer in self.layers if predicate(layer)]
            self.layers = [layer for layer in self.layers if not predicate(layer)]
        for layer in removed:
            _cleanup(layer.source)
        return removed

    def snapshot(self) -> list[Layer]:
        with self._lock:
            return list(self.layers)

    @property
    def has_layers(self) -> bool:
        with self._lock:
            return bool(self.layers)

    # -- called from the player thread -------------------------------------

    def read(self) -> bytes:
        callback: Callable[[Exception | None], None] | None = None
        finished: list[Any] = []

        with self._lock:
            main, paused, layers = self.main, self.main_paused, list(self.layers)

        mixed = SILENCE
        if main is not None and not paused:
            frame = _safe_read(main)
            if len(frame) < FRAME_BYTES:
                with self._lock:
                    if self.main is main:
                        callback, self._on_main_end = self._on_main_end, None
                        self.main = None
                        self._handover = MAIN_HANDOVER_FRAMES
                finished.append(main)
            if frame:
                mixed = add_frames(mixed, frame)

        ended_layers: list[Layer] = []
        for layer in layers:
            frame = _safe_read(layer.source)
            if len(frame) < FRAME_BYTES and layer.reopen is not None:
                finished.append(layer.source)
                try:
                    layer.source = layer.reopen()
                    frame = frame + _safe_read(layer.source)[: FRAME_BYTES - len(frame)]
                except Exception:  # noqa: BLE001 - a broken loop ends, the mix goes on
                    log.exception("Could not loop %s", layer.title)
                    layer.reopen = None
            if len(frame) < FRAME_BYTES and layer.reopen is None:
                ended_layers.append(layer)
            if frame:
                mixed = add_frames(mixed, frame)

        with self._lock:
            if ended_layers:
                self.layers = [layer for layer in self.layers if layer not in ended_layers]
                finished.extend(layer.source for layer in ended_layers)
            if self._handover > 0:
                self._handover -= 1
            idle = self.main is None and not self.layers and self._handover == 0
            if idle:
                self.closed = True

        for source in finished:
            _cleanup(source)
        if callback is not None:
            _call(callback)
        return b"" if idle else mixed

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._lock:
            self.closed = True
            main, layers = self.main, list(self.layers)
            self.main, self.layers, self._on_main_end = None, [], None
        if main is not None:
            _cleanup(main)
        for layer in layers:
            _cleanup(layer.source)


def _safe_read(source: Any) -> bytes:
    try:
        return source.read() or b""
    except Exception:  # noqa: BLE001 - one broken layer must not stop the rest
        log.exception("An audio layer failed to read")
        return b""


def _cleanup(source: Any) -> None:
    try:
        source.cleanup()
    except Exception:  # noqa: BLE001 - teardown is best effort
        log.debug("Ignored an error cleaning up an audio source")


def _call(callback: Callable[[Exception | None], None]) -> None:
    try:
        callback(None)
    except Exception:  # noqa: BLE001 - the callback only schedules onto the loop
        log.exception("Track-end callback failed")
