"""Delivering transcripts that came back from the transcriber.

This is the other end of the handover. The transcriber writes
`inbox/<session_id>/` with the rendered transcript and a DONE marker; this
module notices, posts the transcript to the channel the session came from, and
releases the audio the recorder was holding for collection.

Nothing here transcribes. This host does not have the CPU for it, and after the
split it does not have the code either.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import outbox, paths
from .contract import DONE_MARKER, TRANSCRIPT_JSON, TRANSCRIPT_MD, done_sessions
from .timeutil import to_iso, utcnow

log = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    """Raised when a returned transcript cannot be used."""


@dataclass
class Delivery:
    session_id: str
    transcript_md: Path
    transcript_json: Path | None
    posted: bool


def collect(inbox_root: Path) -> list[Path]:
    """Session directories the transcriber has finished with."""
    return done_sessions(inbox_root)


def adopt(inbox_dir: Path, sessions_root: Path, session_id: str) -> tuple[Path, Path | None]:
    """Move the returned transcript into the session's own directory.

    Keeping it there is what makes `/session transcript <id>` work months later,
    long after the inbox entry is gone.
    """
    source_md = Path(inbox_dir) / TRANSCRIPT_MD
    if not source_md.is_file():
        raise DeliveryError(f"{inbox_dir} has no {TRANSCRIPT_MD}")

    target_md = paths.transcript_md_path(sessions_root, session_id)
    target_md.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_md, target_md)

    target_json: Path | None = None
    source_json = Path(inbox_dir) / TRANSCRIPT_JSON
    if source_json.is_file():
        target_json = paths.transcript_json_path(sessions_root, session_id)
        shutil.copy2(source_json, target_json)

    return target_md, target_json


def summarize(transcript_md: Path) -> str:
    """Pull the header the transcriber already wrote, rather than re-deriving it."""
    wanted = ("- **Duration:**", "- **Speakers:**", "- **Words:**")
    lines = []
    for line in transcript_md.read_text(encoding="utf-8").splitlines():
        if line.startswith(wanted):
            lines.append(line.replace("- **", "").replace("**", ""))
        if line.startswith("## Transcript"):
            break
    return " | ".join(lines)


class InboxDelivery:
    """Posts returned transcripts and cleans up after them."""

    def __init__(self, db, config, notifier) -> None:  # noqa: ANN001 - avoids import cycles
        self.db = db
        self.config = config
        self.notifier = notifier

    async def deliver_one(self, inbox_dir: Path) -> Delivery | None:
        session_id = inbox_dir.name
        session: dict[str, Any] | None = await self.db.get_session(session_id)
        if session is None:
            log.error(
                "A transcript came back for unknown session %s; leaving it in the inbox",
                session_id,
            )
            return None

        transcript_md, transcript_json = adopt(inbox_dir, self.config.sessions_dir, session_id)

        name = session.get("name") or session_id[:8]
        message = (
            f"Transcript ready for session **{name}** (`{session_id}`)\n"
            f"{summarize(transcript_md)}"
        )
        await self.notifier.notify_session(session, message, transcript_md)

        expires_at = utcnow() + timedelta(days=self.config.audio_retention_days)
        await self.db.update_session(session_id, transcribed=1, audio_expires_at=to_iso(expires_at))
        await self.db.mark_session_state(session_id, "done", finished_at=to_iso(utcnow()))

        # The transcript is delivered and the transcriber keeps the archive, so
        # the staged audio has done its job.
        outbox.discard(self.config.outbox_dir, session_id)
        shutil.rmtree(inbox_dir, ignore_errors=True)

        log.info("Delivered transcript for session %s", session_id)
        return Delivery(
            session_id=session_id,
            transcript_md=transcript_md,
            transcript_json=transcript_json,
            posted=True,
        )

    async def run_once(self) -> list[Delivery]:
        """Deliver every transcript waiting in the inbox."""
        delivered: list[Delivery] = []
        for inbox_dir in collect(self.config.inbox_dir):
            try:
                result = await self.deliver_one(inbox_dir)
            except Exception as exc:  # noqa: BLE001 - one bad delivery must not stop the rest
                log.exception("Could not deliver transcript from %s", inbox_dir)
                # Leave it in place, but stop retrying it every few seconds.
                _quarantine(inbox_dir, str(exc))
                continue
            if result is not None:
                delivered.append(result)
        return delivered


def _quarantine(inbox_dir: Path, reason: str) -> None:
    """Take a failed delivery out of the polling loop, keeping it for inspection."""
    marker = Path(inbox_dir) / DONE_MARKER
    marker.unlink(missing_ok=True)
    try:
        (Path(inbox_dir) / "DELIVERY_FAILED").write_text(reason, encoding="utf-8")
    except OSError:  # pragma: no cover - best effort
        log.exception("Could not write a failure marker in %s", inbox_dir)
