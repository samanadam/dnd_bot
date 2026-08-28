"""Deferred transcription queue worker.

Transcription is the only genuinely heavy thing this bot does, and a self-hosted
box usually runs other services alongside it. So it is: (a) deferred to a quiet-hours
window, (b) strictly one job at a time — the worker is a single task that awaits
each job to completion before looking at the next one, and (c) persisted in
SQLite so a redeploy loses nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from .config import Config
from .db import Database
from .interfaces import Notifier
from .jobs import run_job
from .quiet_hours import in_quiet_hours, seconds_until_window
from .timeutil import format_duration, to_iso, utcnow
from .transcription import Transcriber

log = logging.getLogger(__name__)


class TranscriptionWorker:
    def __init__(
        self,
        db: Database,
        config: Config,
        transcriber: Transcriber,
        notifier: Notifier,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.transcriber = transcriber
        self.notifier = notifier
        self._clock = clock or (lambda: datetime.now(config.tz))
        self._stop = asyncio.Event()

    # -- scheduling --------------------------------------------------------

    def window_open(self, now: datetime | None = None) -> bool:
        now = now or self._clock()
        return in_quiet_hours(
            now,
            self.config.quiet_hours_start,
            self.config.quiet_hours_end,
            self.config.quiet_hours_enabled,
        )

    def wait_seconds(self, now: datetime | None = None) -> float:
        now = now or self._clock()
        until_open = seconds_until_window(
            now,
            self.config.quiet_hours_start,
            self.config.quiet_hours_end,
            self.config.quiet_hours_enabled,
        )
        # Sleep until the window opens, but wake up at least every poll interval
        # so a restart mid-window still notices work promptly.
        return min(max(until_open, 1.0), float(self.config.queue_poll_seconds))

    # -- single job --------------------------------------------------------

    async def run_once(self) -> bool:
        """Process at most one pending job. Returns True if one was attempted."""
        if not self.window_open():
            return False
        job = await self.db.next_pending_job()
        if job is None:
            return False

        job_id = int(job["id"])
        session_id = job["session_id"]
        session = await self.db.get_session(session_id)
        if session is None:
            await self.db.mark_job(
                job_id, "failed", last_error="Session row disappeared", finished_at=to_iso(utcnow())
            )
            log.error("Queue job %s references missing session %s", job_id, session_id)
            return True

        await self.db.mark_job(job_id, "processing", started_at=to_iso(utcnow()))
        await self.db.increment_attempts(job_id)
        name = session.get("name") or session_id[:8]
        await self.notifier.notify_session(
            session, f"Starting transcription for session **{name}** now. This may take a while."
        )
        log.info("Transcribing session %s (job %s)", session_id, job_id)

        try:
            result = await run_job(
                session,
                self.transcriber,
                sessions_root=self.config.sessions_dir,
                audio_format=self.config.audio_format,
                language=self.config.transcribe_language,
                tz=self.config.tz,
                prompt_extra=self.config.whisper_prompt_extra,
                chunk_seconds=self.config.transcribe_chunk_minutes * 60,
            )
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            log.exception("Transcription job %s failed", job_id)
            await self.db.mark_job(
                job_id,
                "failed",
                last_error=f"{type(exc).__name__}: {exc}",
                finished_at=to_iso(utcnow()),
            )
            await self.notifier.notify_failure(
                session,
                f"Transcription **failed** for session `{session_id}` ({name}).\n"
                f"Error: `{type(exc).__name__}: {exc}`\n"
                f"Audio is still on disk. Retry with `/session recover {session_id}`.",
            )
            return True

        expires_at = utcnow() + timedelta(days=self.config.audio_retention_days)
        await self.db.update_session(session_id, transcribed=1, audio_expires_at=to_iso(expires_at))
        await self.db.mark_job(
            job_id,
            "done",
            finished_at=to_iso(utcnow()),
            last_error="; ".join(result.warnings) or None,
        )

        summary = (
            f"Transcript ready for session **{name}** (`{session_id}`)\n"
            f"Duration: {format_duration(result.duration_seconds)} | "
            f"Speakers: {result.speaker_count} | Words: {result.word_count}\n"
            f"Raw audio will be deleted after {self.config.audio_retention_days} days "
            f"- use `/session export {session_id}` to keep a copy."
        )
        if result.warnings:
            summary += "\nWarnings: " + "; ".join(result.warnings)
        await self.notifier.notify_session(session, summary, result.transcript_md)
        log.info("Session %s transcribed (%s segments)", session_id, result.segment_count)
        return True

    async def drain(self, limit: int | None = None) -> int:
        """Process pending jobs sequentially while the window stays open."""
        processed = 0
        while limit is None or processed < limit:
            if not await self.run_once():
                break
            processed += 1
        return processed

    # -- loop --------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info(
            "Transcription worker started (quiet hours %s: %s-%s %s)",
            "on" if self.config.quiet_hours_enabled else "off",
            self.config.quiet_hours_start.strftime("%H:%M"),
            self.config.quiet_hours_end.strftime("%H:%M"),
            self.config.timezone_name,
        )
        while not self._stop.is_set():
            try:
                await self.drain()
            except Exception:  # noqa: BLE001 - the loop must outlive any single failure
                log.exception("Transcription worker loop error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.wait_seconds())
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()
