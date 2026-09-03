"""Delivering transcripts that came back from the transcriber.

This is the other end of the handover. The transcriber writes
`inbox/<session_id>/` with the rendered transcript and a DONE marker; this
module notices, posts the transcript to the channel the session came from, and
releases the audio the recorder was holding for collection.

Nothing here transcribes. This host does not have the CPU for it, and after the
split it does not have the code either.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import outbox, paths
from .contract import DONE_MARKER, TRANSCRIPT_JSON, TRANSCRIPT_MD, done_sessions
from .r2 import INBOX_PREFIX, R2Store
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


class InboxFetcher:
    """Brings transcripts down from R2 into the local inbox.

    Deliberately stops there rather than delivering them itself: everything
    downstream - adopting the transcript, posting it, updating the database -
    already works against a local directory, and keeping the download as a
    separate step means the two storage backends share one delivery path.

    Downloads land in a staging directory outside the inbox and are renamed in
    once complete. Otherwise `DONE` - which sorts before `transcript.md` - would
    appear first and the delivery pass could pick up a half-downloaded session.
    """

    def __init__(self, store: R2Store, config) -> None:  # noqa: ANN001 - Config
        self.store = store
        self.config = config

    @property
    def staging_dir(self) -> Path:
        return self.config.data_dir / ".inbox-staging"

    def _fetch(self, session_id: str) -> None:
        """Blocking. Download, move into place, then release the R2 copy."""
        staging = self.staging_dir
        staging.mkdir(parents=True, exist_ok=True)
        scratch = staging / session_id
        shutil.rmtree(scratch, ignore_errors=True)

        downloaded = self.store.download_session(INBOX_PREFIX, session_id, staging)
        if not (downloaded / TRANSCRIPT_MD).is_file():
            shutil.rmtree(downloaded, ignore_errors=True)
            raise DeliveryError(f"R2 inbox entry {session_id} has no {TRANSCRIPT_MD}")

        downloaded.rename(self.config.inbox_dir / session_id)
        # Safe to drop: the transcript is on this disk now, and the transcriber
        # keeps its own archive.
        self.store.delete_session(INBOX_PREFIX, session_id)

    async def run_once(self) -> list[str]:
        """Fetch every finished transcript. Returns the ids now waiting locally."""
        fetched: list[str] = []
        try:
            session_ids = await asyncio.to_thread(
                self.store.marked_sessions, INBOX_PREFIX, DONE_MARKER
            )
        except Exception:  # noqa: BLE001 - an R2 outage must not kill the loop
            log.exception("Could not list the R2 inbox")
            return []

        for session_id in session_ids:
            if (self.config.inbox_dir / session_id).exists():
                # Already waiting locally, possibly mid-delivery. Leave both alone.
                continue
            try:
                await asyncio.to_thread(self._fetch, session_id)
            except Exception:  # noqa: BLE001 - one bad transcript must not stop the rest
                log.exception("Could not fetch transcript %s from R2", session_id)
                continue
            fetched.append(session_id)
            log.info("Fetched transcript for session %s from R2", session_id)
        return fetched


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
