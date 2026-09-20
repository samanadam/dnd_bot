"""URL safety for anything that reaches ffmpeg or an extractor.

Two different jobs live here, and they guard opposite ends of the same pipe.

*Input* URLs come from an API caller. They are checked against a host
allowlist, because yt-dlp will fetch whatever it is given - including
`http://169.254.169.254/`, which on a cloud host is the metadata service.

*Output* URLs come back from yt-dlp and are handed to ffmpeg. They are checked
for scheme and for resolving to a public address, because a hostile or
compromised extractor result would otherwise be a request made by this bot,
from inside the network, to anywhere.

Neither check is a substitute for the other: an allowlisted page can still
return a stream URL pointing at a private address.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Input hosts a caller may ask for.
ALLOWED_INPUT_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

SOUNDCLOUD_HOSTS = frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})

# ffmpeg understands protocols we never want reachable through a URL: file://
# reads the disk, concat: chains inputs, and several others open sockets.
# These are the only ones any track needs.
STREAM_PROTOCOLS = "https,tls,tcp,crypto,http,hls,httpproxy"
LOCAL_PROTOCOLS = "file"

MAX_URL_LENGTH = 2048

# A YouTube video id is exactly this. Anything that matches cannot carry a host,
# a path, a query string or an option flag, so a link is rebuilt from it rather
# than trusted.
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


# A SoundCloud track is `<artist>/<track>`, both plain permalink slugs. Anything
# with more or fewer parts (sets, profiles, private-link tokens) is not a single
# public track, and a link is rebuilt from the two slugs rather than trusted.
SOUNDCLOUD_SLUG = r"[A-Za-z0-9_-]{1,120}"
SOUNDCLOUD_LINK = re.compile(
    rf"^https://(?:www\.|m\.)?soundcloud\.com/({SOUNDCLOUD_SLUG})/({SOUNDCLOUD_SLUG})/?$"
)
# Pages that look like `<artist>/<track>` but are not a track.
SOUNDCLOUD_NOT_A_TRACK = frozenset(
    {
        "sets", "likes", "tracks", "albums", "reposts", "following", "followers",
        "comments", "popular-tracks", "spotlight", "playlists", "sounds", "people",
        "users", "groups", "recommended", "new", "you", "discover", "search",
        "stream", "upload", "charts", "stations", "feed", "settings",
        "notifications", "messages", "pages", "mobile", "pro", "go", "jobs",
    }
)  # fmt: skip


class UnsafeUrl(ValueError):
    """A URL that must not be fetched or handed to ffmpeg."""


def _parse(url: str):
    if not isinstance(url, str) or not url:
        raise UnsafeUrl("Empty URL.")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeUrl("URL is too long.")
    # A newline in a URL can inject a header into some clients, and ffmpeg
    # takes header arguments. Nothing legitimate carries one.
    if any(character in url for character in "\r\n\t\x00"):
        raise UnsafeUrl("URL contains control characters.")
    try:
        return urlparse(url)
    except ValueError as exc:
        raise UnsafeUrl("URL is malformed.") from exc


def check_input_url(url: str, hosts: frozenset[str] = ALLOWED_INPUT_HOSTS) -> str:
    """A URL a caller asked us to resolve. Allowlisted hosts, https only."""
    parsed = _parse(url)
    if parsed.scheme != "https":
        raise UnsafeUrl("Only https links are accepted.")
    host = (parsed.hostname or "").lower()
    if host not in hosts:
        raise UnsafeUrl("That host is not on the allowlist.")
    return url


def canonical_watch_url(video_id: str) -> str:
    """The one URL form the bot fetches for a video id."""
    if not isinstance(video_id, str) or not YOUTUBE_ID.fullmatch(video_id):
        raise UnsafeUrl("That is not a YouTube video id.")
    return f"https://www.youtube.com/watch?v={video_id}"


def soundcloud_track_path(url: str) -> str | None:
    """`artist/track` (lower case) for a plain SoundCloud track link, else None."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        return None
    match = SOUNDCLOUD_LINK.fullmatch(url)
    if match is None:
        return None
    artist, track = match.group(1).lower(), match.group(2).lower()
    if track in SOUNDCLOUD_NOT_A_TRACK or artist in SOUNDCLOUD_NOT_A_TRACK:
        return None
    return f"{artist}/{track}"


def canonical_soundcloud_url(path: str) -> str:
    """The one URL form the bot fetches for a SoundCloud track."""
    if not isinstance(path, str) or soundcloud_track_path(f"https://soundcloud.com/{path}") != path:
        raise UnsafeUrl("That is not a SoundCloud track.")
    return f"https://soundcloud.com/{path}"


def _addresses(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrl("Could not resolve the stream host.") from exc
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except ValueError:  # pragma: no cover - getaddrinfo returns literals
            continue
    if not found:
        raise UnsafeUrl("Could not resolve the stream host.")
    return found


def check_stream_url(url: str, resolver=_addresses) -> str:
    """A URL an extractor produced, about to be handed to ffmpeg.

    Every address the host resolves to must be public. Checking one and using
    another is the whole trick behind DNS rebinding, so this rejects a host
    that resolves to a mix.

    This is not airtight: ffmpeg resolves the name again when it connects, and
    a record that changes in between would slip past. It stops the realistic
    case - a URL that simply points inward - and the protocol whitelist below
    stops it reading the disk either way.
    """
    parsed = _parse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrl("A stream URL must be http or https.")
    host = parsed.hostname or ""
    if not host:
        raise UnsafeUrl("Stream URL has no host.")

    for address in resolver(host):
        if not address.is_global or address.is_multicast:
            raise UnsafeUrl("Stream URL resolves to a non-public address.")
    return url


def ffmpeg_protocol_args(local: bool) -> str:
    """Restrict ffmpeg to the protocols this source actually needs.

    Without it, `-i` accepts file://, concat: and a long tail of others, so any
    control over the URI becomes a file read.
    """
    allowed = LOCAL_PROTOCOLS if local else STREAM_PROTOCOLS
    return f"-protocol_whitelist {allowed}"


def looks_like_a_flag(uri: str) -> bool:
    """A URI starting with '-' would be read as an ffmpeg option, not a value."""
    return uri.startswith("-")
