"""The whole recording lifecycle, driven end to end against fake Discord objects.

Every other test in this suite exercises a module. None of them drive
`SessionManager.start()`, which is how a reference to a setting that no longer
existed survived into production: the bot joined the voice channel and then
raised before writing the session row.

So this one walks the real path - start, receive packets, stop, encode, stage,
upload, deliver - with nothing faked below the recorder itself except Discord
and the S3 client. ffmpeg is real, so the Opus encode is real; tests that need
it skip when it is missing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_r2 import FakeS3

from dnd_bot.audio import BYTES_PER_SECOND
from dnd_bot.contract import READY_MARKER, is_marked, read_metadata
from dnd_bot.db import Database
from dnd_bot.inbox import InboxDelivery
from dnd_bot.r2 import OUTBOX_PREFIX, R2Store
from dnd_bot.recorder import RecordingError, SessionManager
from dnd_bot.uploader import OutboxUploader

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required to encode Opus"
)


# -- the smallest Discord that `start()` will accept -----------------------


class FakePermissions:
    def __init__(self, connect=True, view_channel=True, move_members=True) -> None:
        self.connect = connect
        self.view_channel = view_channel
        self.move_members = move_members


class FakeMember:
    def __init__(self, member_id: int, name: str, nick: str | None = None, bot: bool = False):
        self.id = member_id
        self.name = name
        self.nick = nick
        self.bot = bot


class FakeVoiceClient:
    def __init__(self) -> None:
        self.recording = False
        self.sink = None
        self.disconnected = False
        self._connected = True

    def start_recording(self, sink, callback, *args) -> None:
        self.recording = True
        self.sink = sink

    def stop_recording(self) -> None:
        self.recording = False

    def is_connected(self) -> bool:
        return self._connected

    async def disconnect(self, force: bool = False) -> None:
        self.disconnected = True
        self._connected = False

    # Test helper: deliver audio the way py-cord's receive thread would.
    def speak(self, member: FakeMember, seconds: float) -> None:
        self.sink.write(b"\x00\x01" * int(seconds * BYTES_PER_SECOND / 2), member)


class FakeGuild:
    def __init__(self) -> None:
        self.id = 1
        self.me = FakeMember(999, "bot", bot=True)


class FakeChannel:
    def __init__(self, members: list[FakeMember], permissions: FakePermissions | None = None):
        self.id = 2
        self.name = "Table"
        self.guild = FakeGuild()
        self.members = members
        self.user_limit = 0
        self._permissions = permissions or FakePermissions()
        self.voice_client = FakeVoiceClient()

    def permissions_for(self, member) -> FakePermissions:
        return self._permissions

    async def connect(self, timeout=None, reconnect=True) -> FakeVoiceClient:
        return self.voice_client


class FakeNotifier:
    def __init__(self) -> None:
        self.channel_messages: list[tuple[int | None, str, Path | None]] = []
        self.dms: list[tuple[int | None, str]] = []

    async def send_channel(self, channel_id, content, file_path=None) -> bool:
        self.channel_messages.append((channel_id, content, file_path))
        return True

    async def send_dm(self, user_id, content) -> bool:
        self.dms.append((user_id, content))
        return True

    # These two mirror DiscordNotifier exactly, including the int() cast: ids
    # come back from SQLite as strings, and a fake that quietly accepted them
    # would hide a real type error rather than catch it.
    async def notify_session(self, session, content, file_path=None) -> None:
        channel_id = session.get("text_channel_id")
        await self.send_channel(int(channel_id) if channel_id else None, content, file_path)

    async def notify_failure(self, session, content) -> None:
        channel_id = session.get("text_channel_id")
        await self.send_channel(int(channel_id) if channel_id else None, content)


@pytest.fixture
async def manager(config):
    config.ensure_dirs()
    db = Database(config.db_path)
    await db.connect()
    yield SessionManager(bot=None, db=db, config=config), db, config
    await db.close()


THORIN = FakeMember(10, "eren", nick="Thorin")
ELENYA = FakeMember(11, "aylin", nick=None)


# -- start ------------------------------------------------------------------


async def test_start_creates_the_session_row_and_begins_recording(manager):
    mgr, db, config = manager
    channel = FakeChannel([THORIN, ELENYA])

    session = await mgr.start(
        channel=channel, text_channel_id=3, invoker=THORIN, name="Kamp Gecesi"
    )

    assert channel.voice_client.recording
    row = await db.get_session(session.session_id)
    assert row is not None, "the session row must exist before recording starts"
    assert row["name"] == "Kamp Gecesi"
    assert row["channel_name"] == "Table"
    assert session.labels == {"10": "Thorin", "11": "aylin"}
    await mgr.stop(1, 2)


async def test_start_refuses_a_second_session_in_the_same_channel(manager):
    mgr, _, _ = manager
    channel = FakeChannel([THORIN])
    await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name=None)

    with pytest.raises(RecordingError, match="already being recorded"):
        await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name=None)
    await mgr.stop(1, 2)


async def test_start_refuses_without_permission_to_connect(manager):
    mgr, db, _ = manager
    channel = FakeChannel([THORIN], FakePermissions(connect=False))

    with pytest.raises(RecordingError, match="permission to connect"):
        await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name=None)
    assert not channel.voice_client.recording


async def test_start_refuses_when_the_disk_cannot_hold_the_session(manager, monkeypatch):
    mgr, _, config = manager
    monkeypatch.setattr("dnd_bot.capacity.free_bytes", lambda path: 1_000_000)
    channel = FakeChannel([THORIN, ELENYA])

    with pytest.raises(RecordingError, match="Not enough disk space"):
        await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name=None)
    assert not channel.voice_client.recording, "must refuse before joining voice"


async def test_a_tight_disk_starts_the_session_but_warns(manager, monkeypatch):
    mgr, _, config = manager
    from dnd_bot import capacity

    needed = capacity.required_bytes(2, config.expected_session_hours)
    monkeypatch.setattr("dnd_bot.capacity.free_bytes", lambda path: int(needed * 1.1))
    channel = FakeChannel([THORIN, ELENYA])

    session = await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name=None)
    assert any("tight" in w for w in session.warnings)
    await mgr.stop(1, 2)


# -- record and stop --------------------------------------------------------


@needs_ffmpeg
async def test_a_recorded_session_is_encoded_and_staged_for_the_transcriber(manager):
    mgr, db, config = manager
    channel = FakeChannel([THORIN, ELENYA])
    session = await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name="Oyun")

    channel.voice_client.speak(THORIN, 0.5)
    channel.voice_client.speak(ELENYA, 0.5)

    result = await mgr.stop(1, 2)

    assert result is not None
    assert result.enqueued, f"staging failed: {result.warnings}"
    assert sorted(result.speakers) == ["Thorin", "aylin"]
    assert channel.voice_client.disconnected

    staged = config.outbox_dir / session.session_id
    assert is_marked(staged, READY_MARKER)
    metadata = read_metadata(staged)
    assert metadata.participants == {"10": "Thorin", "11": "aylin"}
    assert metadata.language == "tr"
    assert sorted(p.name for p in staged.glob("*.opus")) == ["10.opus", "11.opus"]

    row = await db.get_session(session.session_id)
    assert row["completed"] == 1
    # The raw capture is consumed, so the disk is released.
    assert not list((config.sessions_dir / session.session_id / "audio" / "raw").glob("*.pcm"))


async def test_stopping_a_session_nobody_spoke_in_reports_it(manager):
    mgr, _, _ = manager
    channel = FakeChannel([THORIN])
    await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name=None)

    result = await mgr.stop(1, 2)

    assert not result.enqueued
    assert any("No audio" in w for w in result.warnings)


async def test_cancel_deletes_everything(manager):
    mgr, db, config = manager
    channel = FakeChannel([THORIN])
    session = await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name=None)
    channel.voice_client.speak(THORIN, 0.2)

    cancelled = await mgr.cancel(1, 2)

    assert cancelled == session.session_id
    assert not (config.sessions_dir / session.session_id).exists()


# -- all the way to R2 and back --------------------------------------------


@needs_ffmpeg
async def test_the_whole_handover_from_recording_to_posted_transcript(manager):
    """start -> speak -> stop -> upload -> transcript comes back -> posted."""
    mgr, db, config = manager
    store = R2Store(FakeS3(), "bucket")
    channel = FakeChannel([THORIN, ELENYA])

    session = await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name="Oyun")
    channel.voice_client.speak(THORIN, 0.4)
    channel.voice_client.speak(ELENYA, 0.4)
    await mgr.stop(1, 2)

    uploaded = await OutboxUploader(store, config).run_once()
    assert uploaded == [session.session_id]
    assert not (config.outbox_dir / session.session_id).exists(), "local copy released"
    keys = store.list_keys(f"{OUTBOX_PREFIX}/{session.session_id}/")
    assert f"{OUTBOX_PREFIX}/{session.session_id}/{READY_MARKER}" in keys
    assert any(k.endswith("10.opus") for k in keys)

    # The transcriber returns a transcript.
    returned = config.inbox_dir / session.session_id
    returned.mkdir(parents=True)
    (returned / "transcript.md").write_text(
        "# Oyun\n- **Duration:** 4h\n- **Speakers:** 2\n## Transcript\n", encoding="utf-8"
    )
    (returned / "DONE").write_text("", encoding="utf-8")

    notifier = FakeNotifier()
    delivered = await InboxDelivery(db, config, notifier).run_once()

    assert [d.session_id for d in delivered] == [session.session_id]
    channel_id, content, attachment = notifier.channel_messages[0]
    assert channel_id == 3
    assert "Transcript ready" in content
    assert attachment.name == "transcript.md"
    row = await db.get_session(session.session_id)
    assert row["transcribed"] == 1
    assert not returned.exists(), "the inbox entry is consumed"


# -- crash recovery ---------------------------------------------------------


@needs_ffmpeg
async def test_recover_finalizes_a_session_a_crash_left_open(manager):
    """The path every restart after a long game depends on."""
    mgr, db, config = manager
    channel = FakeChannel([THORIN, ELENYA])
    session = await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name="Yarim")
    channel.voice_client.speak(THORIN, 0.4)
    channel.voice_client.speak(ELENYA, 0.4)

    # Simulate a hard crash: flush what the sink holds, then drop the process
    # state without ever going through stop().
    session.sink.cleanup()
    mgr.active.clear()
    if session.monitor_task is not None:
        session.monitor_task.cancel()

    row = await db.get_session(session.session_id)
    assert row["completed"] == 0, "a crashed session stays open"

    from dnd_bot.recovery import scan_for_recoverable

    recoverable = await scan_for_recoverable(db, config.sessions_dir)
    assert [r["id"] for r in recoverable] == [session.session_id]

    result = await mgr.recover(session.session_id)

    assert result.enqueued, f"recovery failed to stage: {result.warnings}"
    assert sorted(result.speakers) == ["Thorin", "aylin"]
    staged = config.outbox_dir / session.session_id
    assert is_marked(staged, READY_MARKER)
    assert sorted(p.name for p in staged.glob("*.opus")) == ["10.opus", "11.opus"]
    assert (await db.get_session(session.session_id))["completed"] == 1

    # Nothing is left to recover a second time.
    assert await scan_for_recoverable(db, config.sessions_dir) == []


async def test_recover_refuses_a_session_that_is_still_recording(manager):
    mgr, _, _ = manager
    channel = FakeChannel([THORIN])
    session = await mgr.start(channel=channel, text_channel_id=3, invoker=THORIN, name=None)

    with pytest.raises(RecordingError, match="still recording"):
        await mgr.recover(session.session_id)
    await mgr.stop(1, 2)


async def test_recover_refuses_an_unknown_session(manager):
    mgr, _, _ = manager
    with pytest.raises(RecordingError, match="No session found"):
        await mgr.recover("does-not-exist")
