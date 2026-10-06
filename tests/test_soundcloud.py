"""SoundCloud as a second yt-dlp source.

No network and no yt-dlp: the runner is a fake that answers the metadata call
with JSON and the download call by writing the file yt-dlp would. What is tested
is what surrounds it - which links are accepted, that a link is rebuilt rather
than trusted, that YouTube and SoundCloud cannot be confused, and that a bad
download leaves nothing behind.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dnd_bot.net import (
    SOUNDCLOUD_HOSTS,
    UnsafeUrl,
    canonical_soundcloud_set_url,
    canonical_soundcloud_url,
    check_input_url,
    soundcloud_set_path,
    soundcloud_track_path,
)
from dnd_bot.tracks import build_sources
from dnd_bot.ytdlp import (
    SET_TRACK_LIMIT,
    SoundCloudResolver,
    SourceDisabled,
    TrackResolutionError,
    YtDlpResolver,
    _title_from_slug,
    classify,
)

PAGE = "https://soundcloud.com/wolfbravery/tavern-ambience"

INFO = {
    "id": "268474219",
    "extractor_key": "Soundcloud",
    "webpage_url": PAGE,
    "title": "Tavern Ambience",
    "duration": 700.2,
    "is_live": False,
    "url": "https://cf-media.sndcdn.com/x.mp3?Policy=abc",
}


class FakeYtDlp:
    def __init__(self, info=None, *, size=2_000, suffix="mp3", code=0, stderr=b"", entries=None):
        self.info = dict(INFO if info is None else info)
        self.entries = entries
        self.size = size
        self.suffix = suffix
        self.code = code
        self.stderr = stderr
        self.metadata_calls: list[list[str]] = []
        self.download_calls: list[list[str]] = []

    async def __call__(self, args, timeout):
        if "--dump-single-json" in args:
            self.metadata_calls.append(args)
            if self.entries is not None:
                return 0, json.dumps({"_type": "playlist", "entries": self.entries}).encode(), b""
            return 0, json.dumps(self.info).encode(), b""
        self.download_calls.append(args)
        template = args[args.index("-o") + 1]
        target = Path(template.replace("%%", "%").replace("%(ext)s", self.suffix))
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.code == 0:
            target.write_bytes(b"x" * self.size)
        return self.code, b"", self.stderr


@pytest.fixture
def sc_config(config):
    config.ensure_dirs()
    return replace(
        config,
        music_enabled=True,
        music_youtube_enabled=True,
        music_soundcloud_enabled=True,
        music_resolve_timeout_seconds=1.0,
        music_yt_download_timeout_seconds=1.0,
        music_yt_layer_max_mb=1,
        music_yt_ambience_max_seconds=1800.0,
        disk_warning_threshold_mb=0,
    )


def make(config, fake=None, duration=700.0):
    fake = fake or FakeYtDlp()
    return SoundCloudResolver(config, runner=fake, prober=lambda path: duration), fake


def saved_files(config):
    directory = config.music_cache_dir / "soundcloud"
    return sorted(p.name for p in directory.glob("*")) if directory.exists() else []


# -- links -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://soundcloud.com/wolfbravery/tavern-ambience", "wolfbravery/tavern-ambience"),
        ("https://www.soundcloud.com/WolfBravery/Tavern-Ambience", "wolfbravery/tavern-ambience"),
        ("https://m.soundcloud.com/a_b/c-d/", "a_b/c-d"),
    ],
)
def test_a_plain_track_link_reduces_to_artist_and_track(url, expected):
    assert soundcloud_track_path(url) == expected
    assert canonical_soundcloud_url(expected) == f"https://soundcloud.com/{expected}"


@pytest.mark.parametrize(
    "url",
    [
        "http://soundcloud.com/a/b",
        "https://soundcloud.com/a",
        "https://soundcloud.com/a/b/c",
        "https://soundcloud.com/a/sets/mix",
        "https://soundcloud.com/a/likes",
        "https://soundcloud.com/a/tracks",
        "https://soundcloud.com/discover/sets",
        "https://soundcloud.com/a/b?in=x",
        "https://soundcloud.com/a/b#t=1",
        "https://soundcloud.com/a/b/s-SECRET",
        "https://soundcloud.com/a b/c",
        "https://soundcloud.com/../etc",
        "https://soundcloud.com.evil.example/a/b",
        "https://evil.example/soundcloud.com/a/b",
        "https://user:pw@soundcloud.com/a/b",
        "https://soundcloud.com:444/a/b",
        "https://soundcloud.com/a/b\n--exec=x",
        "https://soundcloud.com/" + "a" * 121 + "/b",
        "https://www.youtube.com/watch?v=abcdefghijk",
        "-o /etc/passwd",
        "",
        None,
        5,
    ],
)
def test_anything_else_is_not_a_track_link(url):
    assert soundcloud_track_path(url) is None


@pytest.mark.parametrize("path", ["A/b", "a/b/c", "a", "a/sets", "a/b?x", "../a"])
def test_a_canonical_url_is_only_built_from_a_clean_path(path):
    with pytest.raises(UnsafeUrl):
        canonical_soundcloud_url(path)


def test_each_service_only_accepts_its_own_hosts():
    assert check_input_url(PAGE, SOUNDCLOUD_HOSTS) == PAGE
    with pytest.raises(UnsafeUrl):
        check_input_url(PAGE)  # the YouTube default
    with pytest.raises(UnsafeUrl):
        check_input_url("https://www.youtube.com/watch?v=abcdefghijk", SOUNDCLOUD_HOSTS)


# -- search and streaming --------------------------------------------------


def entry(path, **over):
    return {
        "id": "1",
        "url": "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A1",
        "webpage_url": f"https://soundcloud.com/{path}",
        "title": path,
        "duration": 90,
        **over,
    }


async def test_search_uses_the_soundcloud_prefix_and_returns_canonical_links(sc_config):
    fake = FakeYtDlp(entries=[entry("a/one"), entry("B/Two")])
    resolver, _ = make(sc_config, fake)
    tracks = await resolver.search("tavern", 3)

    assert [t.id for t in tracks] == [
        "https://soundcloud.com/a/one",
        "https://soundcloud.com/b/two",
    ]
    assert {t.source for t in tracks} == {"soundcloud"}
    assert all(t.uri == "" for t in tracks)
    args = fake.metadata_calls[0]
    assert "scsearch3:tavern" in args
    assert args[-2:][0] == "--"


async def test_search_drops_hits_that_are_not_single_tracks(sc_config):
    fake = FakeYtDlp(
        entries=[
            entry("a/one"),
            entry("a/sets/mix"),
            entry("x/y", webpage_url="https://evil.example/x/y"),
            entry("x/z", webpage_url=None),
            None,
        ]
    )
    resolver, _ = make(sc_config, fake)
    assert [t.id for t in await resolver.search("x")] == ["https://soundcloud.com/a/one"]


async def test_an_eleven_digit_track_id_is_not_mistaken_for_a_youtube_id(sc_config, monkeypatch):
    monkeypatch.setattr("dnd_bot.ytdlp.check_stream_url", lambda url: url)
    fake = FakeYtDlp({**INFO, "id": "12345678901"})
    resolver, _ = make(sc_config, fake)
    track = await resolver.resolve(PAGE)
    assert track.id == PAGE
    assert "youtube" not in track.id


async def test_resolve_returns_a_stream_and_prefers_a_plain_file(sc_config, monkeypatch):
    monkeypatch.setattr("dnd_bot.ytdlp.check_stream_url", lambda url: url)
    resolver, fake = make(sc_config)
    track = await resolver.resolve(PAGE)

    assert track.source == "soundcloud"
    assert track.id == PAGE
    assert track.uri.startswith("https://cf-media.sndcdn.com/")
    assert track.expires_at is not None
    args = fake.metadata_calls[0]
    assert args[args.index("-f") + 1].startswith("bestaudio[protocol^=http]")
    assert args[-1] == PAGE


async def test_resolve_refuses_a_youtube_link_and_a_playlist(sc_config):
    resolver, fake = make(sc_config)
    with pytest.raises(TrackResolutionError, match="allowlist"):
        await resolver.resolve("https://www.youtube.com/watch?v=abcdefghijk")
    assert fake.metadata_calls == []

    playlist = FakeYtDlp(entries=[entry("a/one")])
    resolver, _ = make(sc_config, playlist)
    with pytest.raises(TrackResolutionError, match="playlist"):
        await resolver.resolve("https://soundcloud.com/a/sets/mix")


async def test_youtube_still_refuses_a_soundcloud_link(sc_config):
    resolver = YtDlpResolver(sc_config, runner=FakeYtDlp())
    with pytest.raises(TrackResolutionError, match="allowlist"):
        await resolver.resolve(PAGE)


# -- sounds ----------------------------------------------------------------


async def test_a_sound_is_downloaded_once_into_its_own_folder(sc_config):
    resolver, fake = make(sc_config)
    track = await resolver.fetch_layer(PAGE, "ambience")

    assert track.source == "soundcloud"
    assert track.id == PAGE
    assert track.title == "Tavern Ambience"
    assert track.duration_seconds == 700.0
    saved = Path(track.uri)
    assert saved.parent == sc_config.music_cache_dir / "soundcloud"
    assert saved.name.startswith("sc_") and saved.suffix == ".mp3"
    assert len(saved.stem) == 3 + 24
    # Named from a hash, so nothing from the link reaches the file system.
    assert "wolfbravery" not in saved.name

    again = await resolver.fetch_layer(PAGE, "ambience")
    assert again.uri == track.uri
    assert len(fake.download_calls) == 1
    assert len(fake.metadata_calls) == 1


async def test_a_differently_written_link_is_the_same_sound(sc_config):
    resolver, fake = make(sc_config)
    first = await resolver.fetch_layer(PAGE, "ambience")
    second = await resolver.fetch_layer(
        "https://www.soundcloud.com/WolfBravery/Tavern-Ambience/", "ambience"
    )
    assert second.uri == first.uri
    assert second.id == PAGE
    assert len(fake.download_calls) == 1


async def test_the_download_is_asked_for_by_the_rebuilt_url(sc_config):
    resolver, fake = make(sc_config)
    await resolver.fetch_layer("https://m.soundcloud.com/WolfBravery/Tavern-Ambience", "ambience")
    args = fake.download_calls[0]
    assert args[-2:] == ["--", PAGE]
    assert args[args.index("--max-filesize") + 1] == "1M"
    assert "--ignore-config" in args and "--no-playlist" in args
    assert args[args.index("-f") + 1].startswith("bestaudio[protocol^=http]")


@pytest.mark.parametrize(
    "link",
    [
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://soundcloud.com/a/sets/mix",
        "https://soundcloud.com/a/b?in=evil",
        "http://soundcloud.com/a/b",
        "https://soundcloud.com/a",
        "--exec=touch x",
        "",
    ],
)
async def test_a_link_that_is_not_a_track_is_refused_before_any_network_call(sc_config, link):
    resolver, fake = make(sc_config)
    with pytest.raises(TrackResolutionError):
        await resolver.fetch_layer(link, "sfx")
    assert fake.metadata_calls == [] and fake.download_calls == []
    assert saved_files(sc_config) == []


async def test_a_youtube_sound_and_a_soundcloud_sound_do_not_share_files(sc_config):
    video = "abcdefghijk"
    youtube_info = {"id": video, "title": "Door", "duration": 12, "is_live": False}
    yt = YtDlpResolver(
        sc_config, runner=FakeYtDlp(youtube_info, suffix="m4a"), prober=lambda path: 12.0
    )
    sc, _ = make(sc_config)
    a = await yt.fetch_layer(f"https://www.youtube.com/watch?v={video}", "sfx")
    b = await sc.fetch_layer(PAGE, "ambience")
    assert Path(a.uri).parent != Path(b.uri).parent
    assert video in a.uri and video not in b.uri


@pytest.mark.parametrize(
    "change, message",
    [
        ({"extractor_key": "Youtube"}, "did not resolve to the track it names"),
        ({"webpage_url": "https://soundcloud.com/other/track"}, "did not resolve"),
        ({"webpage_url": "https://evil.example/wolfbravery/tavern-ambience"}, "did not resolve"),
        ({"is_live": True}, "live stream"),
        ({"duration": None}, "no known length"),
        ({"duration": 4000}, "longer than the 30 minute limit"),
    ],
)
async def test_what_the_extractor_reports_is_checked_before_downloading(sc_config, change, message):
    fake = FakeYtDlp({**INFO, **change})
    resolver, _ = make(sc_config, fake)
    with pytest.raises(TrackResolutionError, match=message):
        await resolver.fetch_layer(PAGE, "ambience")
    assert fake.download_calls == []
    assert saved_files(sc_config) == []


async def test_a_playlist_answer_is_refused(sc_config):
    fake = FakeYtDlp(entries=[entry("a/one")])
    resolver, _ = make(sc_config, fake)
    with pytest.raises(TrackResolutionError, match="playlist"):
        await resolver.fetch_layer(PAGE, "ambience")
    assert fake.download_calls == []


async def test_effects_have_the_short_limit(sc_config):
    resolver, fake = make(sc_config)
    with pytest.raises(TrackResolutionError, match="60 second limit for effects"):
        await resolver.fetch_layer(PAGE, "sfx")
    assert fake.download_calls == []


async def test_a_download_that_is_too_large_or_the_wrong_length_leaves_nothing(sc_config):
    fake = FakeYtDlp(size=2_000_000)
    resolver, _ = make(sc_config, fake)
    with pytest.raises(TrackResolutionError, match="larger than the 1 MB limit"):
        await resolver.fetch_layer(PAGE, "ambience")
    assert saved_files(sc_config) == []

    resolver, _ = make(sc_config, FakeYtDlp(), duration=5000.0)
    with pytest.raises(TrackResolutionError, match="not playable audio"):
        await resolver.fetch_layer(PAGE, "ambience")
    assert saved_files(sc_config) == []


async def test_a_failed_download_is_reported_in_soundcloud_terms(sc_config):
    fake = FakeYtDlp(
        code=1, stderr=b"ERROR: Unable to download JSON metadata: HTTP Error 404: Not Found"
    )
    resolver, _ = make(sc_config, fake)
    with pytest.raises(TrackResolutionError, match="not found"):
        await resolver.fetch_layer(PAGE, "ambience")
    assert saved_files(sc_config) == []


# -- switches --------------------------------------------------------------


async def test_the_source_can_be_switched_off(sc_config):
    resolver = SoundCloudResolver(replace(sc_config, music_soundcloud_enabled=False))
    with pytest.raises(SourceDisabled, match="SoundCloud"):
        await resolver.search("x")
    with pytest.raises(SourceDisabled):
        await resolver.fetch_layer(PAGE, "sfx")


async def test_it_needs_the_ytdlp_switch_too(sc_config):
    resolver = SoundCloudResolver(replace(sc_config, music_youtube_enabled=False))
    with pytest.raises(SourceDisabled):
        await resolver.resolve(PAGE)


def test_sources_are_registered_by_their_switches(sc_config):
    both = build_sources(sc_config, None)
    assert {"youtube", "soundcloud"} <= set(both)
    assert isinstance(both["soundcloud"], SoundCloudResolver)

    only_youtube = build_sources(replace(sc_config, music_soundcloud_enabled=False), None)
    assert "soundcloud" not in only_youtube and "youtube" in only_youtube

    neither = build_sources(replace(sc_config, music_youtube_enabled=False), None)
    assert "soundcloud" not in neither and "youtube" not in neither


# -- messages --------------------------------------------------------------


def test_messages_name_the_right_service():
    assert "SoundCloud" in classify("HTTP Error 429", "SoundCloud")
    assert "track" in classify("This video is unavailable", "SoundCloud")
    assert "video" in classify("This video is unavailable")
    assert "not found" in classify("HTTP Error 404: Not Found")
    assert "private" in classify("HTTP Error 403: Forbidden")


def test_a_word_containing_age_is_not_an_age_restriction():
    assert "age-restricted" not in classify("Unable to download webpage: timed out")
    assert "age-restricted" in classify("Sign in to confirm your age")


# -- sets (albums and playlists) -------------------------------------------

SET = "https://soundcloud.com/wolfbravery/sets/tavern-nights"


@pytest.mark.parametrize(
    "url, expected",
    [
        (SET, "wolfbravery/sets/tavern-nights"),
        (
            "https://www.soundcloud.com/WolfBravery/sets/Tavern-Nights/",
            "wolfbravery/sets/tavern-nights",
        ),
        ("https://m.soundcloud.com/a_b/sets/c-d", "a_b/sets/c-d"),
    ],
)
def test_a_public_set_link_reduces_to_its_path(url, expected):
    assert soundcloud_set_path(url) == expected
    assert canonical_soundcloud_set_url(expected) == f"https://soundcloud.com/{expected}"


@pytest.mark.parametrize(
    "url",
    [
        PAGE,
        "http://soundcloud.com/a/sets/b",
        "https://soundcloud.com/a/sets",
        "https://soundcloud.com/a/sets/b/s-SECRETTOKEN",
        "https://soundcloud.com/discover/sets/weekly",
        "https://soundcloud.com/a/sets/" + "b" * 116,
        "https://soundcloud.com.evil.example/a/sets/b",
        "https://on.soundcloud.com/AbCdEf",
        "https://soundcloud.com/a/sets/b?si=1",
    ],
)
def test_anything_but_a_public_set_link_is_refused(url):
    assert soundcloud_set_path(url) is None


def test_a_track_link_is_never_a_set_and_a_set_never_a_track():
    assert soundcloud_track_path(SET) is None
    assert soundcloud_set_path(PAGE) is None
    with pytest.raises(UnsafeUrl):
        canonical_soundcloud_set_url("wolfbravery/tavern-ambience")


class FakeSetYtDlp:
    def __init__(self, listing):
        self.listing = listing
        self.calls: list[list[str]] = []

    async def __call__(self, args, timeout):
        self.calls.append(args)
        return 0, json.dumps(self.listing).encode(), b""


def set_listing(entries, title="Tavern Nights"):
    return {"_type": "playlist", "title": title, "entries": entries}


async def test_a_set_lists_its_tracks_without_resolving_them(sc_config):
    fake = FakeSetYtDlp(
        set_listing(
            [
                {
                    "url": "https://soundcloud.com/wolfbravery/tavern-ambience",
                    "title": "Tavern Ambience",
                    "duration": 700,
                },
                {"url": "https://soundcloud.com/wolfbravery/rainy-night"},
            ]
        )
    )
    resolver = SoundCloudResolver(sc_config, runner=fake)
    listing = await resolver.set_entries(SET)

    assert listing.title == "Tavern Nights"
    assert [t.id for t in listing.tracks] == [
        "https://soundcloud.com/wolfbravery/tavern-ambience",
        "https://soundcloud.com/wolfbravery/rainy-night",
    ]
    assert [t.title for t in listing.tracks] == ["Tavern Ambience", "Rainy night"]
    assert listing.tracks[0].duration_seconds == 700.0
    # No stream yet, and already stale: the player resolves each in turn.
    assert all(t.uri == "" and t.is_stale(0.0) for t in listing.tracks)
    args = fake.calls[0]
    assert args[-1] == SET and args[-2] == "--"
    assert "--flat-playlist" in args and "--yes-playlist" in args
    assert args.index("--yes-playlist") > args.index("--no-playlist")


async def test_a_set_link_is_rebuilt_not_trusted(sc_config):
    fake = FakeSetYtDlp(set_listing([{"url": PAGE}]))
    await SoundCloudResolver(sc_config, runner=fake).set_entries(
        "https://www.soundcloud.com/WolfBravery/sets/Tavern-Nights/"
    )
    assert fake.calls[0][-1] == SET


async def test_set_entries_that_are_not_plain_tracks_are_dropped(sc_config):
    fake = FakeSetYtDlp(
        set_listing(
            [
                {"url": PAGE},
                {"url": "https://api-v2.soundcloud.com/tracks/123?secret_token=x"},
                {"url": "https://api-v2.soundcloud.com/users/123"},
                {"url": "https://soundcloud.com/a/b/s-SECRET"},
                {"url": "https://evil.example/a/b"},
                None,
            ]
        )
    )
    listing = await SoundCloudResolver(sc_config, runner=fake).set_entries(SET)
    assert [t.id for t in listing.tracks] == [PAGE]
    assert listing.skipped == 5


async def test_a_long_set_is_cut_at_the_limit(sc_config):
    entries = [
        {"url": f"https://soundcloud.com/a/t{index}"} for index in range(SET_TRACK_LIMIT + 1)
    ]
    fake = FakeSetYtDlp(set_listing(entries))
    listing = await SoundCloudResolver(sc_config, runner=fake).set_entries(SET)
    assert len(listing.tracks) == SET_TRACK_LIMIT
    assert listing.truncated is True
    assert fake.calls[0][fake.calls[0].index("--playlist-end") + 1] == str(SET_TRACK_LIMIT + 1)


async def test_a_set_with_nothing_playable_is_refused(sc_config):
    fake = FakeSetYtDlp(set_listing([{"url": "https://api.soundcloud.com/tracks/1"}]))
    with pytest.raises(TrackResolutionError, match="no playable"):
        await SoundCloudResolver(sc_config, runner=fake).set_entries(SET)


async def test_something_that_is_not_a_set_is_refused(sc_config):
    fake = FakeSetYtDlp(INFO)
    with pytest.raises(TrackResolutionError, match="not a SoundCloud set"):
        await SoundCloudResolver(sc_config, runner=fake).set_entries(SET)


@pytest.mark.parametrize(
    "link", [PAGE, "https://soundcloud.com/a/sets/b/s-x", "https://evil.example/a/sets/b", 7]
)
async def test_a_bad_set_link_never_reaches_yt_dlp(sc_config, link):
    fake = FakeSetYtDlp(set_listing([{"url": PAGE}]))
    with pytest.raises(TrackResolutionError):
        await SoundCloudResolver(sc_config, runner=fake).set_entries(link)
    assert fake.calls == []


async def test_sets_follow_the_soundcloud_switch(sc_config):
    fake = FakeSetYtDlp(set_listing([{"url": PAGE}]))
    resolver = SoundCloudResolver(replace(sc_config, music_soundcloud_enabled=False), runner=fake)
    with pytest.raises(SourceDisabled):
        await resolver.set_entries(SET)


def test_set_titles_are_sanitized(sc_config):
    assert _title_from_slug("a/rainy_night--2") == "Rainy night 2"


async def test_numbered_set_entries_are_kept_and_named_by_their_place(sc_config):
    """SoundCloud lists only the first few tracks of a set in full."""
    fake = FakeSetYtDlp(
        set_listing(
            [{"url": PAGE}, {"url": "https://api-v2.soundcloud.com/tracks/1378976746"}],
            title="Out of spite",
        )
    )
    listing = await SoundCloudResolver(sc_config, runner=fake).set_entries(SET)
    assert [t.id for t in listing.tracks] == [
        PAGE,
        "https://api-v2.soundcloud.com/tracks/1378976746",
    ]
    assert listing.tracks[1].title == "Out of spite · track 2"
    assert listing.skipped == 0


async def test_a_numbered_track_resolves_to_its_real_page(sc_config):
    fake = FakeYtDlp()
    resolver = SoundCloudResolver(sc_config, runner=fake)
    track = await resolver.resolve("https://api-v2.soundcloud.com/tracks/268474219")
    assert track.id == PAGE
    assert track.title == "Tavern Ambience"
    assert fake.metadata_calls[0][-1] == "https://api-v2.soundcloud.com/tracks/268474219"


@pytest.mark.parametrize(
    "link",
    [
        "https://api-v2.soundcloud.com/tracks/1?secret_token=x",
        "https://api-v2.soundcloud.com/tracks/abc",
        "https://api-v2.soundcloud.com/users/1",
        "http://api-v2.soundcloud.com/tracks/1",
        "https://api-v2.soundcloud.com.evil.example/tracks/1",
    ],
)
async def test_only_the_exact_numbered_form_is_taken(sc_config, link):
    fake = FakeYtDlp()
    with pytest.raises(TrackResolutionError):
        await SoundCloudResolver(sc_config, runner=fake).resolve(link)
    assert fake.metadata_calls == []


async def test_a_numbered_track_that_is_not_a_single_track_is_refused(sc_config):
    fake = FakeYtDlp(info={**INFO, "webpage_url": "https://soundcloud.com/a/sets/b"})
    with pytest.raises(TrackResolutionError):
        await SoundCloudResolver(sc_config, runner=fake).resolve(
            "https://api-v2.soundcloud.com/tracks/1"
        )
