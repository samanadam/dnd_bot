"""URL safety checks.

These guard the two ends of the resolver pipe: what a caller may ask yt-dlp to
fetch, and what ffmpeg may be pointed at afterwards.
"""

from __future__ import annotations

import ipaddress

import pytest

from dnd_bot.net import (
    UnsafeUrl,
    check_input_url,
    check_stream_url,
    ffmpeg_protocol_args,
    looks_like_a_flag,
)


def resolving_to(*addresses):
    return lambda host: [ipaddress.ip_address(a) for a in addresses]


# -- input URLs --------------------------------------------------------------


def test_an_allowlisted_youtube_link_passes():
    url = "https://www.youtube.com/watch?v=abc"
    assert check_input_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=abc",  # not https
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://localhost/admin",
        "https://evil.example/watch?v=abc",
        "file:///etc/passwd",
        "",
    ],
)
def test_unsafe_input_urls_are_refused(url):
    with pytest.raises(UnsafeUrl):
        check_input_url(url)


def test_a_url_with_a_newline_is_refused():
    """ffmpeg takes header arguments; a newline is how you forge one."""
    with pytest.raises(UnsafeUrl):
        check_input_url("https://www.youtube.com/watch?v=a\r\nHost: evil")


def test_an_absurdly_long_url_is_refused():
    with pytest.raises(UnsafeUrl):
        check_input_url("https://www.youtube.com/watch?v=" + "a" * 4000)


# -- stream URLs -------------------------------------------------------------


def test_a_public_stream_url_passes():
    url = "https://rr1---sn-abc.googlevideo.com/videoplayback?x=1"
    assert check_stream_url(url, resolver=resolving_to("142.250.1.1")) == url


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.10",  # private
        "169.254.169.254",  # link-local, cloud metadata
        "::1",  # IPv6 loopback
        "fd00::1",  # IPv6 unique-local
    ],
)
def test_a_stream_url_pointing_inward_is_refused(address):
    with pytest.raises(UnsafeUrl):
        check_stream_url("https://sneaky.example/stream", resolver=resolving_to(address))


def test_a_host_resolving_to_a_mix_is_refused():
    """One public answer and one private one is the rebinding trick."""
    with pytest.raises(UnsafeUrl):
        check_stream_url(
            "https://sneaky.example/stream", resolver=resolving_to("142.250.1.1", "127.0.0.1")
        )


def test_a_non_http_stream_url_is_refused():
    with pytest.raises(UnsafeUrl):
        check_stream_url("file:///etc/passwd", resolver=resolving_to("142.250.1.1"))


def test_an_unresolvable_host_is_refused():
    def fails(host):
        raise UnsafeUrl("Could not resolve the stream host.")

    with pytest.raises(UnsafeUrl):
        check_stream_url("https://nope.example/stream", resolver=fails)


# -- ffmpeg arguments --------------------------------------------------------


def test_a_stream_may_not_read_the_disk():
    assert "file" not in ffmpeg_protocol_args(local=False)
    assert "https" in ffmpeg_protocol_args(local=False)


def test_a_cached_file_may_not_open_a_socket():
    assert ffmpeg_protocol_args(local=True) == "-protocol_whitelist file"


def test_a_uri_that_would_read_as_an_option_is_recognised():
    assert looks_like_a_flag("-i/etc/passwd")
    assert not looks_like_a_flag("https://example.com/a")
