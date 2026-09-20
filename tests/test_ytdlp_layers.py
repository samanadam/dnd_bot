"""Soundboard sounds from YouTube: downloaded once, played from disk.

No yt-dlp and no network: the runner is a fake that answers the metadata call
with JSON and answers the download call by writing the file yt-dlp would. What is
tested is everything around it - which links are accepted, what is refused before
a byte is fetched, what the arguments look like, and that nothing is left behind
when a download goes wrong.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from dnd_bot.config import Config
from dnd_bot.ytdlp import (
    LAYER_DIR,
    SourceDisabled,
    TrackResolutionError,
    YtDlpResolver,
)

VIDEO = "abcdefghijk"
LINK = f"https://www.youtube.com/watch?v={VIDEO}"

INFO = {
    "id": VIDEO,
    "webpage_url": LINK,
    "title": "Door slam",
    "duration": 12,
    "is_live": False,
}


class FakeYtDlp:
    """Answers `--dump-single-json` with metadata and a download with a file."""

    def __init__(
        self,
        info=None,
        *,
        size=2_000,
        suffix="m4a",
        code=0,
        stderr=b"",
        delay=0.0,
        write=True,
        partial_only=False,
    ):
        self.info = dict(INFO if info is None else info)
        self.size = size
        self.suffix = suffix
        self.code = code
        self.stderr = stderr
        self.delay = delay
        self.write = write
        self.partial_only = partial_only
        self.metadata_calls: list[list[str]] = []
        self.download_calls: list[list[str]] = []

    async def __call__(self, args, timeout):
        if "--dump-single-json" in args:
            self.metadata_calls.append(args)
            return 0, json.dumps(self.info).encode(), b""
        self.download_calls.append(args)
        template = args[args.index("-o") + 1]
        target = Path(template.replace("%%", "%").replace("%(ext)s", self.suffix))
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.partial_only:
            Path(str(target) + ".part").write_bytes(b"x" * 10)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.write and self.code == 0:
            target.write_bytes(b"x" * self.size)
        return self.code, b"", self.stderr


@pytest.fixture
def yt_config(config: Config) -> Config:
    config.ensure_dirs()
    return replace(
        config,
        music_enabled=True,
        music_youtube_enabled=True,
        music_resolve_timeout_seconds=1.0,
        music_yt_download_timeout_seconds=1.0,
        music_yt_sfx_max_seconds=60.0,
        music_yt_ambience_max_seconds=1800.0,
        music_yt_layer_max_mb=1,
        # The test disk is not the subject; never let free space decide a test.
        disk_warning_threshold_mb=0,
    )


def make(config, fake=None, duration=12.0):
    fake = fake or FakeYtDlp()
    resolver = YtDlpResolver(config, runner=fake, prober=lambda path: duration)
    return resolver, fake


def layer_files(config):
    directory = config.music_cache_dir / LAYER_DIR
    return sorted(p.name for p in directory.glob("*")) if directory.exists() else []


# -- the happy path --------------------------------------------------------


async def test_an_effect_is_downloaded_once_and_played_from_disk(yt_config):
    resolver, fake = make(yt_config)
    track = await resolver.fetch_layer(LINK, "sfx")

    assert track.source == "youtube"
    assert track.id == LINK
    assert track.title == "Door slam"
    assert track.duration_seconds == 12.0
    assert track.expires_at is None
    saved = Path(track.uri)
    assert saved.is_file()
    assert saved.parent == yt_config.music_cache_dir / LAYER_DIR
    assert saved.name == f"yt_{VIDEO}.m4a"
    assert len(fake.download_calls) == 1


async def test_a_second_request_is_served_from_the_cache_without_the_network(yt_config):
    resolver, fake = make(yt_config)
    first = await resolver.fetch_layer(LINK, "sfx")
    second = await resolver.fetch_layer(LINK, "sfx")

    assert second.uri == first.uri
    assert second.title == "Door slam"
    assert len(fake.metadata_calls) == 1
    assert len(fake.download_calls) == 1


async def test_a_cached_effect_can_also_be_asked_for_as_ambience(yt_config):
    resolver, fake = make(yt_config)
    await resolver.fetch_layer(LINK, "sfx")
    again = await resolver.fetch_layer(LINK, "ambience")
    assert again.id == LINK
    assert len(fake.download_calls) == 1


async def test_a_cached_long_sound_still_meets_the_effect_limit(yt_config):
    fake = FakeYtDlp({**INFO, "duration": 600})
    resolver, _ = make(yt_config, fake, duration=600.0)
    await resolver.fetch_layer(LINK, "ambience")

    with pytest.raises(TrackResolutionError, match="longer than the 60 second limit for effects"):
        await resolver.fetch_layer(LINK, "sfx")
    assert len(fake.download_calls) == 1


async def test_using_a_sound_refreshes_its_place_in_the_cache(yt_config):
    resolver, _ = make(yt_config)
    track = await resolver.fetch_layer(LINK, "sfx")
    path = Path(track.uri)
    os.utime(path, (1_000_000, 1_000_000))
    await resolver.fetch_layer(LINK, "sfx")
    assert path.stat().st_mtime > 1_000_000


# -- what the download command looks like ----------------------------------


async def test_the_download_is_built_from_the_video_id_and_pinned_to_the_cache(yt_config):
    resolver, fake = make(yt_config)
    await resolver.fetch_layer(LINK, "sfx")
    args = fake.download_calls[0]

    # Everything after `--` is the target, and it is the canonical form.
    assert args[-2:] == ["--", LINK]
    for flag in (
        "--ignore-config",
        "--no-playlist",
        "--no-cache-dir",
        "--no-mtime",
        "--no-continue",
    ):
        assert flag in args
    assert args[args.index("--max-filesize") + 1] == "1M"
    template = args[args.index("-o") + 1]
    assert template.startswith(str(yt_config.music_cache_dir / LAYER_DIR))
    assert template.endswith(f"yt_{VIDEO}.%(ext)s")
    # Audio only: a video format must not be a fallback.
    assert "best" not in args[args.index("-f") + 1].split("/")


async def test_a_percent_sign_in_the_data_path_is_escaped_for_yt_dlp(config, tmp_path):
    odd = replace(
        config,
        data_dir=tmp_path / "100%data",
        music_enabled=True,
        music_youtube_enabled=True,
        disk_warning_threshold_mb=0,
        music_yt_layer_max_mb=1,
    )
    odd.ensure_dirs()
    resolver, fake = make(odd)
    await resolver.fetch_layer(LINK, "sfx")
    template = fake.download_calls[0][fake.download_calls[0].index("-o") + 1]
    assert "100%%data" in template


# -- links that must be refused before anything is fetched ------------------


@pytest.mark.parametrize(
    "link",
    [
        f"https://youtu.be/{VIDEO}",
        f"https://music.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/watch?v={VIDEO}&list=PLxyz",
        f"https://www.youtube.com/watch?v={VIDEO}&t=10",
        f"https://www.youtube.com/watch?v={VIDEO[:10]}",
        f"https://www.youtube.com/watch?v={VIDEO}x",
        f"http://www.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/watch?v={VIDEO}\n",
        f"https://evil.example/watch?v={VIDEO}",
        f"https://www.youtube.com.evil.example/watch?v={VIDEO}",
        f"https://user@www.youtube.com/watch?v={VIDEO}",
        "https://www.youtube.com/playlist?list=PLxyz",
        "https://www.youtube.com/@somechannel",
        f"--exec=touch /tmp/x {LINK}",
        "-o /etc/passwd",
        "file:///etc/passwd",
        VIDEO,
        "",
    ],
)
async def test_only_the_plain_watch_link_is_accepted(yt_config, link):
    resolver, fake = make(yt_config)
    with pytest.raises(TrackResolutionError):
        await resolver.fetch_layer(link, "sfx")
    assert fake.metadata_calls == [] and fake.download_calls == []
    assert layer_files(yt_config) == []


async def test_an_unknown_kind_is_refused(yt_config):
    resolver, fake = make(yt_config)
    with pytest.raises(TrackResolutionError):
        await resolver.fetch_layer(LINK, "music")
    assert fake.metadata_calls == []


async def test_it_does_nothing_while_youtube_is_off(config):
    resolver = YtDlpResolver(replace(config, music_youtube_enabled=False))
    with pytest.raises(SourceDisabled):
        await resolver.fetch_layer(LINK, "sfx")


# -- what the metadata rules out --------------------------------------------


@pytest.mark.parametrize(
    ("info", "kind", "message"),
    [
        ({**INFO, "duration": 61}, "sfx", "longer than the 60 second limit for effects"),
        ({**INFO, "duration": 1801}, "ambience", "longer than the 30 minute limit for ambience"),
        ({**INFO, "is_live": True}, "ambience", "live stream"),
        ({**INFO, "_type": "playlist"}, "sfx", "playlist"),
        ({**INFO, "entries": [{"id": "x"}]}, "sfx", "playlist"),
        ({**INFO, "id": "zzzzzzzzzzz"}, "sfx", "did not resolve to the video it names"),
        ({**INFO, "duration": None}, "sfx", "no known length"),
        ({**INFO, "duration": 0}, "sfx", "no known length"),
        ({**INFO, "duration": True}, "sfx", "no known length"),
        ({**INFO, "duration": "12"}, "sfx", "no known length"),
    ],
)
async def test_unsuitable_videos_are_refused_before_downloading(yt_config, info, kind, message):
    fake = FakeYtDlp(info)
    resolver, _ = make(yt_config, fake)
    with pytest.raises(TrackResolutionError, match=message):
        await resolver.fetch_layer(LINK, kind)
    assert fake.download_calls == []
    assert layer_files(yt_config) == []


async def test_the_limits_are_inclusive_at_the_boundary(yt_config):
    fake = FakeYtDlp({**INFO, "duration": 60})
    resolver, _ = make(yt_config, fake, duration=60.0)
    assert (await resolver.fetch_layer(LINK, "sfx")).duration_seconds == 60.0


async def test_the_configured_limits_are_used(yt_config):
    tight = replace(yt_config, music_yt_sfx_max_seconds=5.0)
    resolver, fake = make(tight)
    with pytest.raises(TrackResolutionError, match="5 second limit"):
        await resolver.fetch_layer(LINK, "sfx")
    assert fake.download_calls == []


async def test_a_hostile_title_is_cleaned(yt_config):
    fake = FakeYtDlp({**INFO, "title": "Door\u202e\n slam\x00"})
    resolver, _ = make(yt_config, fake)
    assert (await resolver.fetch_layer(LINK, "sfx")).title == "Door slam"


# -- what goes wrong while downloading --------------------------------------


async def test_a_failed_download_is_explained_and_leaves_nothing_behind(yt_config):
    fake = FakeYtDlp(code=1, stderr=b"ERROR: Private video", partial_only=True)
    resolver, _ = make(yt_config, fake)
    with pytest.raises(TrackResolutionError, match="private"):
        await resolver.fetch_layer(LINK, "sfx")
    assert layer_files(yt_config) == []


async def test_a_download_that_overruns_is_stopped_and_cleaned_up(yt_config):
    fake = FakeYtDlp(delay=2.0, partial_only=True)
    tight = replace(yt_config, music_yt_download_timeout_seconds=0.2)
    resolver, _ = make(tight, fake)
    with pytest.raises(TimeoutError):
        await resolver.fetch_layer(LINK, "sfx")
    assert layer_files(tight) == []


async def test_a_file_over_the_size_limit_is_deleted(yt_config):
    fake = FakeYtDlp(size=1_000_001)
    resolver, _ = make(yt_config, fake)
    with pytest.raises(TrackResolutionError, match="larger than the 1 MB limit"):
        await resolver.fetch_layer(LINK, "sfx")
    assert layer_files(yt_config) == []


async def test_yt_dlp_skipping_an_oversize_file_is_reported(yt_config):
    fake = FakeYtDlp(write=False)
    resolver, _ = make(yt_config, fake)
    with pytest.raises(TrackResolutionError, match="larger than the 1 MB limit"):
        await resolver.fetch_layer(LINK, "sfx")
    assert layer_files(yt_config) == []


async def test_a_format_ffmpeg_would_not_read_from_disk_is_refused(yt_config):
    fake = FakeYtDlp(suffix="mp4")
    resolver, _ = make(yt_config, fake)
    with pytest.raises(TrackResolutionError):
        await resolver.fetch_layer(LINK, "sfx")
    assert layer_files(yt_config) == []


@pytest.mark.parametrize("probed", [None, 0.0, 400.0])
async def test_ffprobe_has_the_last_word(yt_config, probed):
    resolver = YtDlpResolver(yt_config, runner=FakeYtDlp(), prober=lambda path: probed)
    with pytest.raises(TrackResolutionError, match="not playable audio"):
        await resolver.fetch_layer(LINK, "sfx")
    assert layer_files(yt_config) == []


async def test_a_probe_that_crashes_is_not_playable_audio(yt_config):
    def boom(path):
        raise RuntimeError("ffprobe missing")

    resolver = YtDlpResolver(yt_config, runner=FakeYtDlp(), prober=boom)
    with pytest.raises(TrackResolutionError, match="not playable audio"):
        await resolver.fetch_layer(LINK, "sfx")
    assert layer_files(yt_config) == []


async def test_a_tight_disk_refuses_the_download(yt_config):
    full = replace(yt_config, disk_warning_threshold_mb=10**9)
    resolver, fake = make(full)
    with pytest.raises(TrackResolutionError, match="free disk"):
        await resolver.fetch_layer(LINK, "sfx")
    assert fake.download_calls == []


async def test_a_runner_that_cannot_start_is_one_clean_error(yt_config):
    async def broken(args, timeout):
        if "--dump-single-json" in args:
            return 0, json.dumps(INFO).encode(), b""
        raise OSError("/usr/bin/yt-dlp: permission denied")

    resolver = YtDlpResolver(yt_config, runner=broken, prober=lambda path: 12.0)
    with pytest.raises(TrackResolutionError) as caught:
        await resolver.fetch_layer(LINK, "sfx")
    assert "/usr" not in str(caught.value)


# -- the cache stays honest --------------------------------------------------


async def test_a_file_without_its_notes_is_downloaded_again(yt_config):
    resolver, fake = make(yt_config)
    await resolver.fetch_layer(LINK, "sfx")
    (yt_config.music_cache_dir / LAYER_DIR / f"yt_{VIDEO}.json").unlink()

    await resolver.fetch_layer(LINK, "sfx")
    assert len(fake.download_calls) == 2


@pytest.mark.parametrize(
    "notes",
    [
        "not json",
        "[]",
        "{}",
        '{"title": 5, "duration": 12}',
        '{"title": "x", "duration": -1}',
        '{"title": "x", "duration": true}',
        '{"title": "x", "duration": "12"}',
    ],
)
async def test_damaged_notes_are_never_trusted(yt_config, notes):
    resolver, fake = make(yt_config)
    await resolver.fetch_layer(LINK, "sfx")
    (yt_config.music_cache_dir / LAYER_DIR / f"yt_{VIDEO}.json").write_text(notes)

    track = await resolver.fetch_layer(LINK, "sfx")
    assert track.title == "Door slam"
    assert len(fake.download_calls) == 2


async def test_notes_written_by_hand_are_cleaned_like_any_title(yt_config):
    resolver, _ = make(yt_config)
    await resolver.fetch_layer(LINK, "sfx")
    notes = yt_config.music_cache_dir / LAYER_DIR / f"yt_{VIDEO}.json"
    notes.write_text(json.dumps({"title": "Evil\u202e\n", "duration": 12}))
    assert (await resolver.fetch_layer(LINK, "sfx")).title == "Evil"


async def test_notes_are_replaced_atomically_and_leave_no_scratch_file(yt_config):
    resolver, _ = make(yt_config)
    await resolver.fetch_layer(LINK, "sfx")
    assert layer_files(yt_config) == [f"yt_{VIDEO}.json", f"yt_{VIDEO}.m4a"]


async def test_two_requests_for_one_video_share_a_single_download(yt_config):
    fake = FakeYtDlp(delay=0.05)
    resolver, _ = make(yt_config, fake)
    first, second = await asyncio.gather(
        resolver.fetch_layer(LINK, "sfx"), resolver.fetch_layer(LINK, "ambience")
    )
    assert first.uri == second.uri
    assert len(fake.download_calls) == 1
    assert resolver._video_locks == {}


async def test_the_lock_table_does_not_grow_with_failures(yt_config):
    fake = FakeYtDlp({**INFO, "duration": 999})
    resolver, _ = make(yt_config, fake)
    for _ in range(3):
        with pytest.raises(TrackResolutionError):
            await resolver.fetch_layer(LINK, "sfx")
    assert resolver._video_locks == {}


async def test_two_different_videos_do_not_block_each_other(yt_config):
    other = "zyxwvutsrqp"
    seen: list[str] = []

    async def runner(args, timeout):
        if "--dump-single-json" in args:
            target = args[-1]
            seen.append(target)
            video = target.rsplit("=", 1)[1]
            return 0, json.dumps({**INFO, "id": video}).encode(), b""
        template = args[args.index("-o") + 1]
        path = Path(template.replace("%(ext)s", "m4a"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)
        return 0, b"", b""

    resolver = YtDlpResolver(yt_config, runner=runner, prober=lambda path: 5.0)
    one, two = await asyncio.gather(
        resolver.fetch_layer(LINK, "sfx"),
        resolver.fetch_layer(f"https://www.youtube.com/watch?v={other}", "sfx"),
    )
    assert one.uri != two.uri
    assert len(seen) == 2
