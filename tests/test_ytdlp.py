"""The yt-dlp resolver, tested without yt-dlp and without a network.

The runner is injected, so what is exercised here is everything around the
extraction: the guards, the error classification, the expiry arithmetic and the
refusal to trust what comes back.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from dnd_bot.config import Config
from dnd_bot.ytdlp import (
    SourceDisabled,
    TrackResolutionError,
    YtDlpResolver,
    classify,
    sanitize_title,
)

WATCH = "https://www.youtube.com/watch?v=abc"

INFO = {
    "id": "abc",
    "webpage_url": WATCH,
    "title": "Tavern Ambience",
    "url": "https://rr1---sn-x.googlevideo.com/videoplayback?expire=99999999999",
    "duration": 3600,
    "is_live": False,
}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def runner_for(info=None, *, code=0, stderr=b"", stdout=None, delay=0.0, calls=None):
    async def run(args, timeout):
        if calls is not None:
            calls.append(args)
        if delay:
            await asyncio.sleep(delay)
        payload = stdout if stdout is not None else json.dumps(info or INFO).encode()
        return code, payload, stderr

    return run


@pytest.fixture
def youtube_config(config: Config) -> Config:
    return replace(
        config,
        music_enabled=True,
        music_youtube_enabled=True,
        music_resolve_timeout_seconds=0.2,
        music_stream_ttl_seconds=1800.0,
        music_max_track_seconds=10800.0,
    )


@pytest.fixture
def clock():
    return FakeClock()


def resolver(config, clock=None, **kwargs):
    return YtDlpResolver(config, runner=runner_for(**kwargs), clock=clock or FakeClock())


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every googlevideo host resolves to a public address, as far as tests care."""
    import ipaddress

    import dnd_bot.net as net

    monkeypatch.setattr(net, "_addresses", lambda host: [ipaddress.ip_address("142.250.1.1")])
    monkeypatch.setattr(
        "dnd_bot.ytdlp.check_stream_url",
        lambda url: net.check_stream_url(
            url, resolver=lambda h: [ipaddress.ip_address("142.250.1.1")]
        ),
    )


# -- the happy path ----------------------------------------------------------


async def test_resolve_returns_a_playable_track(youtube_config):
    track = await resolver(youtube_config).resolve(WATCH)
    assert track.title == "Tavern Ambience"
    assert track.source == "youtube"
    assert track.uri.startswith("https://")
    assert track.duration_seconds == 3600


async def test_resolve_passes_the_link_after_a_double_dash(youtube_config):
    """`--` is what stops a URL that begins with a dash reading as an option."""
    calls = []
    source = YtDlpResolver(youtube_config, runner=runner_for(calls=calls))
    await source.resolve(WATCH)
    assert calls[0][-2] == "--"
    assert calls[0][-1] == WATCH


async def test_resolve_never_downloads(youtube_config):
    calls = []
    source = YtDlpResolver(youtube_config, runner=runner_for(calls=calls))
    await source.resolve(WATCH)
    assert "--skip-download" in calls[0]
    assert "--no-playlist" in calls[0]
    # A resolver that wrote to this disk would compete with the recording.
    assert "--no-cache-dir" in calls[0]


# -- guards ------------------------------------------------------------------


async def test_a_disabled_resolver_refuses_before_running_anything(config):
    with pytest.raises(SourceDisabled):
        await resolver(config).resolve(WATCH)


async def test_a_non_youtube_link_is_refused(youtube_config):
    """The allowlist runs before yt-dlp sees the string."""
    calls = []
    source = YtDlpResolver(youtube_config, runner=runner_for(calls=calls))
    with pytest.raises(TrackResolutionError):
        await source.resolve("https://169.254.169.254/latest/meta-data")
    assert calls == []


async def test_a_live_stream_is_refused(youtube_config):
    """Anything queued behind a live stream would never play."""
    info = dict(INFO, is_live=True)
    with pytest.raises(TrackResolutionError, match="live"):
        await resolver(youtube_config, info=info).resolve(WATCH)


async def test_a_live_stream_can_be_allowed(youtube_config):
    config = replace(youtube_config, music_allow_live=True)
    track = await resolver(config, info=dict(INFO, is_live=True)).resolve(WATCH)
    assert track.title == "Tavern Ambience"


async def test_an_overlong_track_is_refused(youtube_config):
    info = dict(INFO, duration=20000)
    with pytest.raises(TrackResolutionError, match="longer than"):
        await resolver(youtube_config, info=info).resolve(WATCH)


async def test_a_result_with_no_audio_is_refused(youtube_config):
    info = {k: v for k, v in INFO.items() if k != "url"}
    with pytest.raises(TrackResolutionError, match="no playable audio"):
        await resolver(youtube_config, info=info).resolve(WATCH)


async def test_a_format_list_is_searched_for_audio(youtube_config):
    info = {
        **{k: v for k, v in INFO.items() if k != "url"},
        "formats": [
            {"acodec": "none", "url": "https://rr1---sn-x.googlevideo.com/video"},
            {"acodec": "opus", "url": "https://rr1---sn-x.googlevideo.com/audio"},
        ],
    }
    track = await resolver(youtube_config, info=info).resolve(WATCH)
    assert track.uri.endswith("/audio")


# -- not trusting the output -------------------------------------------------


async def test_a_stream_url_pointing_inward_is_refused(youtube_config, monkeypatch):
    """An allowlisted page can still hand back an internal address."""
    import ipaddress

    import dnd_bot.net as net

    monkeypatch.setattr(
        "dnd_bot.ytdlp.check_stream_url",
        lambda url: net.check_stream_url(
            url, resolver=lambda h: [ipaddress.ip_address("127.0.0.1")]
        ),
    )
    with pytest.raises(TrackResolutionError, match="not safe"):
        await resolver(youtube_config).resolve(WATCH)


async def test_a_file_url_from_the_extractor_is_refused(youtube_config):
    info = dict(INFO, url="file:///etc/passwd")
    with pytest.raises(TrackResolutionError, match="not safe"):
        await resolver(youtube_config, info=info).resolve(WATCH)


async def test_a_hostile_title_is_sanitized(youtube_config):
    info = dict(INFO, title="Tavern" + chr(0x202E) + "evil" + chr(0x00))
    track = await resolver(youtube_config, info=info).resolve(WATCH)
    assert track.title == "Tavernevil"


def test_sanitize_title_caps_the_length():
    assert len(sanitize_title("x" * 500)) <= 200


def test_sanitize_title_always_returns_something():
    assert sanitize_title("") == "Unknown track"
    assert sanitize_title(None) == "Unknown track"


# -- failures ----------------------------------------------------------------


async def test_a_timeout_propagates_so_the_route_can_report_504(youtube_config):
    source = resolver(youtube_config, delay=1.0)
    with pytest.raises(asyncio.TimeoutError):
        await source.resolve(WATCH)


async def test_non_json_output_is_a_resolution_error(youtube_config):
    source = resolver(youtube_config, stdout=b"<html>nope</html>")
    with pytest.raises(TrackResolutionError):
        await source.resolve(WATCH)


async def test_a_failure_does_not_quote_yt_dlp_at_the_caller(youtube_config):
    """yt-dlp's messages carry URLs and sometimes local paths. Log, do not echo."""
    stderr = b"ERROR: [youtube] abc: /home/someone/cookies.txt is unreadable"
    source = resolver(youtube_config, code=1, stderr=stderr)
    with pytest.raises(TrackResolutionError) as caught:
        await source.resolve(WATCH)
    assert "/home/someone" not in str(caught.value)


@pytest.mark.parametrize(
    "stderr, expected",
    [
        (b"ERROR: Private video. Sign in", "private"),
        (b"ERROR: Video unavailable", "unavailable"),
        (b"ERROR: Sign in to confirm you're not a bot", "rate limited"),
        (b"ERROR: HTTP Error 429: Too Many Requests", "rate limiting"),
        (b"ERROR: Unable to extract player response", "needs updating"),
        (b"ERROR: something nobody has seen before", "Could not resolve"),
    ],
)
def test_failures_are_classified_into_something_actionable(stderr, expected):
    assert expected.lower() in classify(stderr.decode()).lower()


# -- expiry ------------------------------------------------------------------


async def test_a_resolved_stream_carries_an_expiry(youtube_config, clock):
    track = await resolver(youtube_config, clock=clock).resolve(WATCH)
    assert track.expires_at is not None
    assert not track.is_stale(clock())


async def test_a_stream_goes_stale_after_the_ttl(youtube_config, clock):
    track = await resolver(youtube_config, clock=clock).resolve(WATCH)
    clock.advance(youtube_config.music_stream_ttl_seconds + 1)
    assert track.is_stale(clock())


async def test_an_expiry_in_the_url_shortens_the_ttl(youtube_config, clock):
    """YouTube's own `expire` beats the configured default when it is sooner."""
    import time as real_time

    soon = int(real_time.time()) + 400
    info = dict(INFO, url=f"https://rr1---sn-x.googlevideo.com/videoplayback?expire={soon}")
    track = await resolver(youtube_config, clock=clock, info=info).resolve(WATCH)
    assert track.expires_at - clock() < youtube_config.music_stream_ttl_seconds


# -- search ------------------------------------------------------------------


async def test_search_returns_results_without_resolving_streams(youtube_config):
    """Resolving every hit would be wasted work, and stale by the time it is clicked."""
    info = {"entries": [{"id": "1", "title": "One"}, {"id": "2", "title": "Two"}]}
    tracks = await resolver(youtube_config, info=info).search("tavern")
    assert [t.title for t in tracks] == ["One", "Two"]
    assert all(t.uri == "" for t in tracks)


async def test_search_caps_how_many_results_it_asks_for(youtube_config):
    calls = []
    source = YtDlpResolver(youtube_config, runner=runner_for(info={"entries": []}, calls=calls))
    await source.search("tavern", limit=500)
    assert "ytsearch10:tavern" in calls[0]


async def test_a_pasted_link_is_resolved_rather_than_searched(youtube_config):
    track = await resolver(youtube_config).search(WATCH)
    assert track[0].uri.startswith("https://")


# -- concurrency -------------------------------------------------------------


async def test_resolutions_are_bounded(youtube_config):
    """This host is recording audio; resolutions must not pile up on the CPU."""
    live = 0
    peak = 0

    async def run(args, timeout):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return 0, json.dumps(INFO).encode(), b""

    config = replace(youtube_config, music_ytdlp_max_concurrent=2, music_resolve_timeout_seconds=5)
    source = YtDlpResolver(config, runner=run)
    await asyncio.gather(*(source.resolve(WATCH) for _ in range(6)))
    assert peak <= 2
