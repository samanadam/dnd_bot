"""Delivering transcripts that come back from the transcriber.

This is the only path by which a transcript now reaches Discord, so it carries
the weight the in-process worker used to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dnd_bot import paths
from dnd_bot.contract import DONE_MARKER, mark
from dnd_bot.db import Database
from dnd_bot.inbox import DeliveryError, InboxDelivery, adopt, collect, summarize
from dnd_bot.outbox import publish
from dnd_bot.timeutil import to_iso, utcnow

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

TRANSCRIPT = """# Kamp Gecesi

- **Date:** 2026-08-29 21:00:00 +03
- **Duration:** 02:00:00
- **Speakers:** 2 (Thorin, Elenya)
- **Words:** 1234

## Transcript

[21:00:00] Thorin: merhaba
"""


@dataclass
class FakeNotifier:
    session_messages: list[tuple[str, Path | None]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    dms: list[tuple[int | None, str]] = field(default_factory=list)

    async def send_channel(self, channel_id, content, file_path=None) -> bool:
        return True

    async def send_dm(self, user_id, content) -> bool:
        self.dms.append((user_id, content))
        return True

    async def notify_session(self, session, content, file_path=None) -> None:
        self.session_messages.append((content, file_path))

    async def notify_failure(self, session, content) -> None:
        self.failures.append(content)


async def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "bot.db", MIGRATIONS)
    await db.connect()
    return db


async def seed_session(db: Database, config, session_id: str = "s1") -> dict:
    await db.create_session(
        session_id=session_id,
        name="Kamp Gecesi",
        guild_id=1,
        channel_id=2,
        channel_name="Genel",
        text_channel_id=3,
        started_by_user_id=10,
        start_time=to_iso(utcnow()),
        participants={"10": "Thorin"},
        language="tr",
        model_used="medium",
    )
    await db.update_session(session_id, completed=1, end_time=to_iso(utcnow()))
    # Stage it the way /session stop does, so there is something to release.
    paths.ensure_session_dirs(config.sessions_dir, session_id)
    paths.finalized_audio_path(config.sessions_dir, session_id, "10", "opus").write_bytes(b"audio")
    row = await db.get_session(session_id)
    publish(
        row,
        sessions_root=config.sessions_dir,
        outbox_root=config.outbox_dir,
        audio_format="opus",
        timezone_name="Europe/Istanbul",
    )
    await db.mark_exported(session_id)
    return row


def deliver_transcript(config, session_id: str = "s1", *, marker: bool = True) -> Path:
    directory = config.inbox_dir / session_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transcript.md").write_text(TRANSCRIPT, encoding="utf-8")
    (directory / "transcript.json").write_text('{"segments": []}', encoding="utf-8")
    if marker:
        mark(directory, DONE_MARKER)
    return directory


@pytest.fixture
async def wired(config, tmp_path):
    config.ensure_dirs()
    db = await make_db(tmp_path)
    notifier = FakeNotifier()
    yield config, db, notifier, InboxDelivery(db, config, notifier)
    await db.close()


# -- collection ------------------------------------------------------------


def test_only_marked_directories_are_collected(config):
    config.ensure_dirs()
    deliver_transcript(config, "s1")
    deliver_transcript(config, "s2", marker=False)  # still being copied

    assert [p.name for p in collect(config.inbox_dir)] == ["s1"]


def test_summary_reuses_the_header_the_transcriber_wrote(tmp_path: Path):
    path = tmp_path / "transcript.md"
    path.write_text(TRANSCRIPT, encoding="utf-8")
    summary = summarize(path)
    assert "Duration: 02:00:00" in summary
    assert "Speakers: 2 (Thorin, Elenya)" in summary
    assert "merhaba" not in summary  # the body is not the summary


def test_adopt_copies_the_transcript_into_the_session(config, tmp_path: Path):
    config.ensure_dirs()
    directory = deliver_transcript(config)

    md, js = adopt(directory, config.sessions_dir, "s1")

    assert md == paths.transcript_md_path(config.sessions_dir, "s1")
    assert md.read_text(encoding="utf-8") == TRANSCRIPT
    assert js is not None and js.is_file()


def test_adopt_refuses_a_directory_with_no_transcript(config):
    config.ensure_dirs()
    empty = config.inbox_dir / "s1"
    empty.mkdir(parents=True)
    with pytest.raises(DeliveryError, match="no transcript.md"):
        adopt(empty, config.sessions_dir, "s1")


# -- delivery --------------------------------------------------------------


async def test_a_returned_transcript_is_posted_and_kept(wired):
    config, db, notifier, deliveries = wired
    await seed_session(db, config)
    deliver_transcript(config)

    delivered = await deliveries.run_once()

    assert [d.session_id for d in delivered] == ["s1"]
    content, attachment = notifier.session_messages[0]
    assert "Transcript ready" in content
    assert "Speakers: 2" in content
    assert attachment == paths.transcript_md_path(config.sessions_dir, "s1")
    # Kept where /session transcript will look for it, months later.
    assert attachment.is_file()


async def test_delivery_marks_the_session_transcribed_and_starts_retention(wired):
    config, db, notifier, deliveries = wired
    await seed_session(db, config)
    deliver_transcript(config)

    await deliveries.run_once()

    row = await db.get_session("s1")
    assert row["transcribed"] == 1
    assert row["audio_expires_at"] is not None
    assert await db.session_state("s1") == "done"
    assert await db.pending_count() == 0


async def test_delivery_releases_the_staged_audio(wired):
    config, db, notifier, deliveries = wired
    await seed_session(db, config)
    assert (config.outbox_dir / "s1").is_dir()
    deliver_transcript(config)

    await deliveries.run_once()

    assert not (config.outbox_dir / "s1").exists()
    assert not (config.inbox_dir / "s1").exists()


async def test_the_same_transcript_is_not_posted_twice(wired):
    config, db, notifier, deliveries = wired
    await seed_session(db, config)
    deliver_transcript(config)

    await deliveries.run_once()
    await deliveries.run_once()

    assert len(notifier.session_messages) == 1


async def test_a_transcript_for_an_unknown_session_is_left_alone(wired):
    config, db, notifier, deliveries = wired
    deliver_transcript(config, "ghost")

    assert await deliveries.run_once() == []
    # Kept for inspection rather than deleted: it is somebody's session.
    assert (config.inbox_dir / "ghost").is_dir()
    assert notifier.session_messages == []


async def test_a_broken_delivery_is_quarantined_not_retried_forever(wired):
    config, db, notifier, deliveries = wired
    await seed_session(db, config)
    directory = config.inbox_dir / "s1"
    directory.mkdir(parents=True)
    mark(directory, DONE_MARKER)  # marked, but no transcript inside

    assert await deliveries.run_once() == []

    assert not (directory / DONE_MARKER).exists()  # out of the polling loop
    assert (directory / "DELIVERY_FAILED").is_file()
    assert await deliveries.run_once() == []  # and stays out


async def test_several_transcripts_are_delivered_in_one_pass(wired):
    config, db, notifier, deliveries = wired
    for session_id in ("s1", "s2"):
        await seed_session(db, config, session_id)
        deliver_transcript(config, session_id)

    delivered = await deliveries.run_once()

    assert sorted(d.session_id for d in delivered) == ["s1", "s2"]
    assert len(notifier.session_messages) == 2
