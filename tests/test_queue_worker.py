"""Worker behaviour: quiet-hours gating, strict serialization, failure reporting."""

from __future__ import annotations

import asyncio
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dnd_bot import paths
from dnd_bot.config import Config
from dnd_bot.db import Database
from dnd_bot.queue_worker import TranscriptionWorker
from dnd_bot.timeutil import to_iso
from dnd_bot.transcription import RawSegment

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
START = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)


def write_silent_wav(path: Path, seconds: float = 1.0) -> None:
    """A real (if silent) WAV, since the pipeline now parses these files."""
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(bytes(int(48000 * seconds) * 4))


@dataclass
class FakeNotifier:
    """Records what would have been sent to Discord."""

    session_messages: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    dms: list[tuple[int | None, str]] = field(default_factory=list)
    channel_messages: list[tuple[int | None, str]] = field(default_factory=list)

    async def send_channel(self, channel_id, content, file_path=None) -> bool:
        self.channel_messages.append((channel_id, content))
        return True

    async def send_dm(self, user_id, content) -> bool:
        self.dms.append((user_id, content))
        return True

    async def notify_session(self, session, content, file_path=None) -> None:
        self.session_messages.append(content)

    async def notify_failure(self, session, content) -> None:
        self.failures.append(content)


class SerialProbe:
    """Fails the test if two transcriptions are ever in flight at once."""

    def __init__(self, delay: float = 0.0) -> None:
        self.concurrent = 0
        self.max_concurrent = 0
        self.order: list[str] = []
        self.prompts: list[str | None] = []
        self.delay = delay

    def transcribe_file(
        self, path: Path, language: str, initial_prompt: str | None = None
    ) -> list[RawSegment]:
        self.prompts.append(initial_prompt)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.order.append(path.parent.parent.name)
        try:
            if self.delay:
                import time

                time.sleep(self.delay)
            return [RawSegment(0.0, 1.0, "merhaba")]
        finally:
            self.concurrent -= 1


class ExplodingTranscriber:
    def transcribe_file(
        self, path: Path, language: str, initial_prompt: str | None = None
    ) -> list[RawSegment]:
        raise RuntimeError("model blew up")


def make_config(tmp_path: Path) -> Config:
    return Config(
        discord_token="t",
        guild_id=1,
        data_dir=tmp_path / "data",
        timezone_name="UTC",
        queue_poll_seconds=1,
    )


async def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "data" / "bot.db", MIGRATIONS)
    await db.connect()
    return db


async def seed_ready_session(db: Database, config: Config, session_id: str) -> None:
    await db.create_session(
        session_id=session_id,
        name=session_id,
        guild_id=1,
        channel_id=2,
        channel_name="Table",
        text_channel_id=3,
        started_by_user_id=10,
        start_time=to_iso(START),
        participants={"10": "Thorin"},
        language="tr",
        model_used="medium",
    )
    await db.update_session(
        session_id, completed=1, end_time=to_iso(START.replace(hour=19)), offsets_json='{"10": 0.0}'
    )
    paths.ensure_session_dirs(config.sessions_dir, session_id)
    write_silent_wav(paths.finalized_audio_path(config.sessions_dir, session_id, "10"))
    await db.enqueue(session_id)


def worker_at(db, config, transcriber, notifier, hour: int) -> TranscriptionWorker:
    return TranscriptionWorker(
        db,
        config,
        transcriber,
        notifier,
        clock=lambda: datetime(2026, 5, 2, hour, 0, tzinfo=UTC),
    )


@pytest.fixture
async def ready(tmp_path):
    config = make_config(tmp_path)
    config.ensure_dirs()
    db = await make_db(tmp_path)
    yield config, db, FakeNotifier()
    await db.close()


async def test_nothing_runs_outside_the_quiet_hours_window(ready):
    config, db, notifier = ready
    await seed_ready_session(db, config, "s1")
    probe = SerialProbe()

    worker = worker_at(db, config, probe, notifier, hour=14)

    assert worker.window_open() is False
    assert await worker.run_once() is False
    assert probe.order == []
    assert await db.pending_count() == 1  # still queued, nothing lost


async def test_jobs_run_inside_the_window(ready):
    config, db, notifier = ready
    await seed_ready_session(db, config, "s1")
    probe = SerialProbe()

    worker = worker_at(db, config, probe, notifier, hour=3)
    assert await worker.run_once() is True

    row = await db.get_session("s1")
    assert row["transcribed"] == 1
    assert row["audio_expires_at"] is not None
    assert paths.transcript_md_path(config.sessions_dir, "s1").exists()
    assert paths.transcript_json_path(config.sessions_dir, "s1").exists()
    assert any("Starting transcription" in m for m in notifier.session_messages)
    # The session's character names are handed to Whisper as an initial prompt.
    assert probe.prompts and "Thorin" in probe.prompts[0]
    assert "Dungeons & Dragons" in probe.prompts[0]
    assert any("Transcript ready" in m for m in notifier.session_messages)


async def test_disabled_quiet_hours_means_the_window_is_always_open(ready):
    config, db, notifier = ready
    config = Config(**{**config.__dict__, "quiet_hours_enabled": False})
    await seed_ready_session(db, config, "s1")

    worker = worker_at(db, config, SerialProbe(), notifier, hour=14)
    assert await worker.run_once() is True


async def test_jobs_are_processed_strictly_one_at_a_time(ready):
    config, db, notifier = ready
    for session_id in ("s1", "s2", "s3"):
        await seed_ready_session(db, config, session_id)
    probe = SerialProbe(delay=0.02)
    worker = worker_at(db, config, probe, notifier, hour=3)

    processed = await worker.drain()

    assert processed == 3
    assert probe.max_concurrent == 1
    assert probe.order == ["s1", "s2", "s3"]  # FIFO by queue time
    assert await db.pending_count() == 0


async def test_drain_stops_when_the_queue_is_empty(ready):
    config, db, notifier = ready
    worker = worker_at(db, config, SerialProbe(), notifier, hour=3)
    assert await worker.drain() == 0


async def test_failure_is_recorded_and_reported_to_both_targets(ready):
    config, db, notifier = ready
    await seed_ready_session(db, config, "s1")
    worker = worker_at(db, config, ExplodingTranscriber(), notifier, hour=3)

    assert await worker.run_once() is True

    cursor = await db.conn.execute("SELECT * FROM transcription_queue WHERE session_id = 's1'")
    job = dict(await cursor.fetchone())
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert "model blew up" in job["last_error"]
    assert notifier.failures and "failed" in notifier.failures[0]

    row = await db.get_session("s1")
    assert row["transcribed"] == 0  # audio is kept so /session recover can retry


async def test_a_missing_session_row_fails_the_job_instead_of_hanging(ready):
    config, db, notifier = ready
    await seed_ready_session(db, config, "s1")
    # Simulates a row removed out of band (manual DB surgery, restored backup).
    await db.conn.execute("PRAGMA foreign_keys=OFF")
    await db.conn.execute("DELETE FROM sessions WHERE id = 's1'")
    await db.conn.commit()
    await db.conn.execute("PRAGMA foreign_keys=ON")

    worker = worker_at(db, config, SerialProbe(), notifier, hour=3)
    assert await worker.run_once() is True
    assert await db.pending_count() == 0


async def test_wait_seconds_is_capped_by_the_poll_interval(ready):
    config, db, notifier = ready
    worker = worker_at(db, config, SerialProbe(), notifier, hour=14)
    assert worker.wait_seconds() == float(config.queue_poll_seconds)


async def test_run_forever_stops_when_asked(ready):
    config, db, notifier = ready
    worker = worker_at(db, config, SerialProbe(), notifier, hour=14)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(task, timeout=3)
