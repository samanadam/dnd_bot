"""Handing staged sessions over to object storage.

`/session stop` still writes the finished session to the local outbox first.
That ordering is deliberate: encoding and staging is the part that must not fail
in the middle of a live session, and the local disk is the only thing available
during a network outage. Uploading is a separate, retryable step that happens
afterwards - and the local copy is released only once R2 confirms it has the
whole session.

So a failed upload costs a delay, never audio.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from .contract import READY_MARKER, ready_sessions
from .r2 import OUTBOX_PREFIX, R2Store, session_prefix

log = logging.getLogger(__name__)


class OutboxUploader:
    """Moves locally staged sessions into R2 and frees the disk they used."""

    def __init__(self, store: R2Store, config, notifier=None) -> None:  # noqa: ANN001 - Config
        self.store = store
        self.config = config
        self.notifier = notifier

    def _already_uploaded(self, session_id: str) -> bool:
        """The READY object exists, so R2 has the complete session."""
        key = session_prefix(OUTBOX_PREFIX, session_id) + READY_MARKER
        return self.store.has_object(key)

    def _upload(self, directory: Path) -> None:
        """Blocking. Runs in a thread; a session can be hundreds of megabytes."""
        session_id = directory.name
        if not self._already_uploaded(session_id):
            count = self.store.upload_session(
                OUTBOX_PREFIX, session_id, directory, marker=READY_MARKER
            )
            log.info("Uploaded session %s to R2 (%s file(s))", session_id, count)
        else:
            log.info("Session %s is already complete in R2; releasing the local copy", session_id)

        # Re-check rather than trusting the writes above: the local audio is
        # about to be deleted, so "R2 says it is there" is the only acceptable
        # justification for that.
        if not self._already_uploaded(session_id):
            raise RuntimeError(f"R2 has no {READY_MARKER} for {session_id} after upload")

    async def run_once(self) -> list[str]:
        """Upload every staged session. Returns the ids now living in R2."""
        uploaded: list[str] = []
        for directory in ready_sessions(self.config.outbox_dir):
            try:
                await asyncio.to_thread(self._upload, directory)
            except Exception as exc:  # noqa: BLE001 - one bad upload must not stop the rest
                log.warning(
                    "Could not upload session %s to R2, keeping it locally to retry: %s",
                    directory.name,
                    exc,
                )
                continue
            shutil.rmtree(directory, ignore_errors=True)
            uploaded.append(directory.name)
        return uploaded
