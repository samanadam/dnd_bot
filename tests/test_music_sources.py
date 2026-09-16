"""Track sources: the R2 prefix, the on-disk cache, and the optional yt-dlp.

The yt-dlp half is tested with an injected fake extractor, so the suite needs
neither the package nor a network. That is the same isolation the runtime has:
if yt-dlp breaks, nothing outside its own source notices.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_r2 import FakeS3

from dnd_bot.config import Config
from dnd_bot.musiccache import CacheFull, cached_path, prune
from dnd_bot.tracks import R2TrackSource, SourceDisabled, TrackResolutionError, build_sources

from dnd_bot.r2 import R2Store  # isort: skip


@pytest.fixture
def music_config(config: Config) -> Config:
    config.ensure_dirs()
    return replace(config, music_enabled=True, music_r2_prefix="music", music_cache_max_mb=1)


class CountingS3(FakeS3):
    """FakeS3 plus call counters, for the caching assertions."""

    def __init__(self):
        super().__init__()
        self.list_calls = 0
        self.download_calls = 0

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None, **kwargs):  # noqa: N803
        self.list_calls += 1
        return super().list_objects_v2(Bucket, Prefix, ContinuationToken, **kwargs)

    def download_file(self, Bucket, Key, Filename):  # noqa: N803
        self.download_calls += 1
        return super().download_file(Bucket, Key, Filename)


@pytest.fixture
def store():
    client = CountingS3()
    client.objects["music/tavern.opus"] = b"a" * 100
    client.objects["music/battle.opus"] = b"b" * 200
    client.objects["music/notes.txt"] = b"not audio"
    client.objects["outbox/session-1/READY"] = b""
    return R2Store(client, "bucket")


@pytest.fixture
def source(store, music_config):
    return R2TrackSource(store, music_config)


# -- listing -----------------------------------------------------------------


async def test_browse_lists_only_audio_under_the_prefix(source):
    titles = [track.title for track in await source.browse()]
    assert titles == ["battle", "tavern"]


async def test_browse_never_reaches_outside_the_prefix(source):
    ids = [track.id for track in await source.browse()]
    assert all(track_id.startswith("music/") for track_id in ids)


async def test_browse_filters_on_a_query(source):
    assert [t.title for t in await source.browse("tav")] == ["tavern"]


async def test_browse_honours_a_limit(source):
    assert len(await source.browse(limit=1)) == 1


async def test_listing_is_cached_between_calls(source, store):
    await source.browse()
    calls = store.client.list_calls
    await source.browse()
    assert store.client.list_calls == calls


# -- resolving ---------------------------------------------------------------


async def test_resolve_downloads_into_the_cache(source, music_config):
    track = await source.resolve("music/tavern.opus")
    path = Path(track.uri)
    assert path.exists()
    assert path.parent == music_config.music_cache_dir
    assert path.read_bytes() == b"a" * 100


async def test_resolve_reuses_a_cached_file(source, store):
    await source.resolve("music/tavern.opus")
    downloads = store.client.download_calls
    await source.resolve("music/tavern.opus")
    assert store.client.download_calls == downloads


async def test_a_key_outside_the_prefix_is_refused(source):
    """An allowlist, not path arithmetic: it simply is not in the listing."""
    with pytest.raises(TrackResolutionError):
        await source.resolve("outbox/session-1/READY")


async def test_a_traversal_id_is_refused(source):
    with pytest.raises(TrackResolutionError):
        await source.resolve("music/../outbox/session-1/READY")


async def test_an_unknown_track_is_refused(source):
    with pytest.raises(TrackResolutionError):
        await source.resolve("music/nope.opus")


async def test_resolve_refuses_to_cache_when_the_disk_is_tight(store, music_config):
    """Raw capture must never lose disk to a cached song."""
    tight = replace(music_config, disk_warning_threshold_mb=10**9)
    with pytest.raises(CacheFull):
        await R2TrackSource(store, tight).resolve("music/tavern.opus")


async def test_a_disabled_source_says_so(music_config):
    with pytest.raises(SourceDisabled):
        await R2TrackSource(None, music_config).browse()


# -- the cache ---------------------------------------------------------------


def test_prune_evicts_the_oldest_until_it_fits(tmp_path: Path):
    for index, name in enumerate(("old.opus", "middle.opus", "new.opus")):
        path = tmp_path / name
        path.write_bytes(b"x" * 600_000)
        import os

        os.utime(path, (1_000 + index, 1_000 + index))

    prune(tmp_path, max_mb=1)
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["new.opus"]


def test_prune_leaves_a_cache_that_already_fits(tmp_path: Path):
    (tmp_path / "one.opus").write_bytes(b"x" * 100)
    assert prune(tmp_path, max_mb=1) == 0
    assert (tmp_path / "one.opus").exists()


def test_a_cached_path_is_flat(tmp_path: Path):
    assert cached_path(tmp_path, "music/sub/track.opus").name == "music_sub_track.opus"


# -- wiring ------------------------------------------------------------------


def test_build_sources_offers_only_what_is_configured(music_config, store):
    assert set(build_sources(music_config, store)) == {"r2"}
    assert set(build_sources(music_config, None)) == set()
    both = build_sources(replace(music_config, music_youtube_enabled=True), store)
    assert set(both) == {"r2", "youtube"}


async def test_resolve_refuses_an_object_that_is_not_audio(source):
    """browse() hides non-audio; resolve() must refuse it, not fetch it for ffmpeg."""
    with pytest.raises(TrackResolutionError, match="not a playable audio file"):
        await source.resolve("music/notes.txt")
