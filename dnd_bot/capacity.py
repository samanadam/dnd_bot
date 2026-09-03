"""Will this session fit on the disk?

Recording writes raw PCM - 192 KB per second per speaker - and only converts it
to Opus at `/session stop`. So the disk high-water mark is not the size of the
finished session but the size of the *whole raw capture*, held until the game
ends, plus one intermediate WAV while each speaker is encoded.

A session that runs out of disk mid-game does not fail loudly:
`DiskSink.write` logs the error and carries on, so the recording silently stops
capturing while everyone keeps playing. That is the worst possible failure mode
and the reason this check exists - refusing at minute zero beats discovering it
at hour four.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from .audio import BYTES_PER_SECOND

log = logging.getLogger(__name__)

# Once free space is under this multiple of the estimate, the session is allowed
# but the channel is told. 1.5 leaves room for the other things sharing the host.
COMFORT_FACTOR = 1.5


@dataclass(frozen=True)
class Estimate:
    """What a session of this shape is expected to need, against what is free."""

    speakers: int
    hours: float
    required_bytes: int
    free_bytes: int

    @property
    def sufficient(self) -> bool:
        return self.free_bytes >= self.required_bytes

    @property
    def comfortable(self) -> bool:
        return self.free_bytes >= self.required_bytes * COMFORT_FACTOR

    @property
    def required_gb(self) -> float:
        return self.required_bytes / 1_000_000_000

    @property
    def free_gb(self) -> float:
        return self.free_bytes / 1_000_000_000


def required_bytes(speakers: int, hours: float) -> int:
    """Peak bytes on disk for a session of this shape.

    One raw capture per speaker for the whole session, plus one more capture's
    worth for the intermediate WAV that `finalize_capture` writes while encoding
    the first speaker - at which point every other speaker's PCM is still on
    disk, because it is deleted only once that speaker's own encode succeeds.
    """
    speakers = max(1, int(speakers))
    per_speaker = max(0.0, float(hours)) * 3600 * BYTES_PER_SECOND
    return int(per_speaker * (speakers + 1))


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(str(path)).free


def estimate(path: Path, speakers: int, hours: float) -> Estimate:
    speakers = max(1, int(speakers))
    return Estimate(
        speakers=speakers,
        hours=float(hours),
        required_bytes=required_bytes(speakers, hours),
        free_bytes=free_bytes(path),
    )


def shortfall_message(est: Estimate) -> str:
    """Why the session was refused, in numbers the operator can act on."""
    return (
        f"Not enough disk space to record safely. A {est.hours:g}-hour session with "
        f"{est.speakers} speaker(s) needs about **{est.required_gb:.1f} GB** of raw "
        f"capture, and only **{est.free_gb:.1f} GB** is free.\n"
        "Raw audio is written uncompressed and is only shrunk at `/session stop`, so "
        "starting now would run the disk out mid-game and silently stop capturing.\n"
        "Free some space, or lower `EXPECTED_SESSION_HOURS` if your games are shorter."
    )


def warning_message(est: Estimate) -> str:
    """Allowed, but close enough that somebody should know."""
    return (
        f"Disk space is tight: about {est.required_gb:.1f} GB needed, "
        f"{est.free_gb:.1f} GB free. The session will start, but keep an eye on it."
    )
