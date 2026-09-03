"""Object-storage handover: uploading staged sessions and fetching transcripts.

Everything here runs against an in-memory stand-in for the S3 client, so the
suite still never touches the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dnd_bot import paths
from dnd_bot.contract import DONE_MARKER, READY_MARKER
from dnd_bot.inbox import InboxFetcher
from dnd_bot.r2 import INBOX_PREFIX, OUTBOX_PREFIX, R2Store, session_prefix
from dnd_bot.uploader import OutboxUploader


class FakeS3:
    """The five calls R2Store makes, backed by a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def upload_file(self, Filename, Bucket, Key):  # noqa: N803 - boto3 spelling
        self.objects[Key] = Path(Filename).read_bytes()
        self.uploads.append(Key)

    def put_object(self, Bucket, Key, Body=b""):  # noqa: N803
        self.objects[Key] = Body
        self.uploads.append(Key)

    def download_file(self, Bucket, Key, Filename):  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)
        target = Path(Filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.objects[Key])

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None, **kwargs):  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in keys]}


@pytest.fixture
def store() -> R2Store:
    return R2Store(FakeS3(), "test-bucket")


def stage_local_outbox(outbox_root: Path, session_id: str, *, marked: bool = True) -> Path:
    directory = outbox_root / session_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.json").write_text('{"schema": 1}', encoding="utf-8")
    (directory / "10.opus").write_bytes(b"audio" * 100)
    (directory / "11.opus").write_bytes(b"audio" * 100)
    if marked:
        (directory / READY_MARKER).write_text("", encoding="utf-8")
    return directory


# -- key layout ------------------------------------------------------------


def test_session_prefix_mirrors_the_directory_layout():
    assert session_prefix(OUTBOX_PREFIX, "s1") == "outbox/s1/"
    assert session_prefix(INBOX_PREFIX, "s1") == "inbox/s1/"


def test_a_session_id_cannot_escape_its_prefix():
    with pytest.raises(ValueError):
        session_prefix(OUTBOX_PREFIX, "../../etc")
    with pytest.raises(ValueError):
        session_prefix(OUTBOX_PREFIX, "a/b")


# -- uploading -------------------------------------------------------------


def test_upload_session_writes_every_file_then_the_marker(store, tmp_path):
    directory = stage_local_outbox(tmp_path / "outbox", "s1")

    uploaded = store.upload_session(OUTBOX_PREFIX, "s1", directory, marker=READY_MARKER)

    assert uploaded == 3  # metadata + two tracks, marker counted separately
    assert store.client.uploads[-1] == "outbox/s1/READY"
    assert set(store.client.objects) == {
        "outbox/s1/metadata.json",
        "outbox/s1/10.opus",
        "outbox/s1/11.opus",
        "outbox/s1/READY",
    }


def test_upload_skips_the_local_marker_so_it_is_never_written_early(store, tmp_path):
    directory = stage_local_outbox(tmp_path / "outbox", "s1")
    store.upload_session(OUTBOX_PREFIX, "s1", directory, marker=READY_MARKER)
    # The marker is the last key written, not one of the bulk uploads.
    assert store.client.uploads.index("outbox/s1/READY") == len(store.client.uploads) - 1


def test_marked_sessions_lists_only_completed_uploads(store, tmp_path):
    store.upload_session(
        OUTBOX_PREFIX, "done", stage_local_outbox(tmp_path / "o", "done"), marker=READY_MARKER
    )
    # A session still copying: files present, no marker.
    partial = stage_local_outbox(tmp_path / "o", "partial", marked=False)
    for item in sorted(partial.iterdir()):
        store.put_file(f"outbox/partial/{item.name}", item)

    assert store.marked_sessions(OUTBOX_PREFIX, READY_MARKER) == ["done"]


# -- downloading -----------------------------------------------------------


def test_download_session_reproduces_the_directory(store, tmp_path):
    store.put_bytes("inbox/s1/transcript.md", b"# Transcript\n")
    store.put_bytes("inbox/s1/transcript.json", b"{}")
    store.put_bytes("inbox/s1/DONE", b"")

    target = store.download_session(INBOX_PREFIX, "s1", tmp_path / "inbox")

    assert (target / "transcript.md").read_text(encoding="utf-8") == "# Transcript\n"
    assert (target / DONE_MARKER).is_file()


def test_delete_session_removes_every_object(store, tmp_path):
    store.upload_session(
        OUTBOX_PREFIX, "s1", stage_local_outbox(tmp_path / "o", "s1"), marker=READY_MARKER
    )
    removed = store.delete_session(OUTBOX_PREFIX, "s1")
    assert removed == 4
    assert store.client.objects == {}


# -- the uploader task -----------------------------------------------------


async def test_uploader_pushes_staged_sessions_and_frees_the_local_copy(store, config):
    config.ensure_dirs()
    directory = stage_local_outbox(config.outbox_dir, "s1")

    uploader = OutboxUploader(store, config)
    assert await uploader.run_once() == ["s1"]

    assert "outbox/s1/READY" in store.client.objects
    assert not directory.exists(), "local staging must be released once R2 confirms the upload"


async def test_uploader_ignores_sessions_that_are_still_being_written(store, config):
    config.ensure_dirs()
    stage_local_outbox(config.outbox_dir, "s1", marked=False)

    assert await uploader_result(OutboxUploader(store, config)) == []
    assert store.client.objects == {}


async def uploader_result(uploader: OutboxUploader) -> list[str]:
    return await uploader.run_once()


async def test_uploader_keeps_the_local_copy_when_the_upload_fails(store, config):
    config.ensure_dirs()
    directory = stage_local_outbox(config.outbox_dir, "s1")

    def explode(*args, **kwargs):
        raise RuntimeError("network is down")

    store.client.upload_file = explode

    uploader = OutboxUploader(store, config)
    assert await uploader.run_once() == []
    assert (directory / READY_MARKER).is_file(), "a failed upload must not lose the audio"


async def test_uploader_does_not_re_upload_a_session_already_in_r2(store, config):
    config.ensure_dirs()
    directory = stage_local_outbox(config.outbox_dir, "s1")
    store.put_bytes("outbox/s1/READY", b"")
    store.client.uploads.clear()  # forget the seeding write

    uploader = OutboxUploader(store, config)
    assert await uploader.run_once() == ["s1"]
    assert not directory.exists()
    assert store.client.uploads == [], "an already-complete session must not be re-sent"


# -- the inbox fetcher -----------------------------------------------------


async def test_fetcher_downloads_finished_transcripts_into_the_local_inbox(store, config):
    config.ensure_dirs()
    store.put_bytes("inbox/s1/transcript.md", b"# Transcript\n")
    store.put_bytes("inbox/s1/DONE", b"")

    fetcher = InboxFetcher(store, config)
    assert await fetcher.run_once() == ["s1"]

    local = config.inbox_dir / "s1"
    assert (local / "transcript.md").is_file()
    assert (local / DONE_MARKER).is_file()
    # Consumed from R2 so the next pass does not fetch it again.
    assert store.marked_sessions(INBOX_PREFIX, DONE_MARKER) == []


async def test_fetcher_leaves_a_transcript_that_is_still_uploading(store, config):
    config.ensure_dirs()
    store.put_bytes("inbox/s1/transcript.md", b"# Transcript\n")  # no DONE yet

    fetcher = InboxFetcher(store, config)
    assert await fetcher.run_once() == []
    assert not (config.inbox_dir / "s1").exists()


async def test_fetcher_does_not_clobber_a_delivery_already_waiting_locally(store, config):
    config.ensure_dirs()
    local = config.inbox_dir / "s1"
    local.mkdir(parents=True)
    (local / "transcript.md").write_text("local copy", encoding="utf-8")
    (local / DONE_MARKER).write_text("", encoding="utf-8")
    store.put_bytes("inbox/s1/transcript.md", b"remote copy")
    store.put_bytes("inbox/s1/DONE", b"")

    fetcher = InboxFetcher(store, config)
    assert await fetcher.run_once() == []
    assert (local / "transcript.md").read_text(encoding="utf-8") == "local copy"


def test_finalized_paths_are_untouched_by_any_of_this(config):
    """Guard: the recording path still writes where it always did."""
    assert paths.raw_pcm_path(config.sessions_dir, "s1", "10").name == "10.pcm"
