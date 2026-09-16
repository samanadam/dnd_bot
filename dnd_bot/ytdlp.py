"""Production yt-dlp resolver.

Split out of `tracks.py` because it needs far more care than the R2 source
does, and none of that care should be able to leak into the path that plays the
table's own files.

The design decisions worth knowing:

* **Subprocess, not a thread.** `asyncio.wait_for` around `to_thread` returns
  control but cannot stop the thread: a wedged yt-dlp call would keep a worker
  and a socket for as long as it liked. A child process can actually be killed,
  and is, on timeout.
* **Bounded concurrency.** Resolutions are CPU- and network-heavy and this host
  is also recording audio. A semaphore keeps them from piling up.
* **Stream URLs expire.** YouTube signs them, typically for a few hours and
  against the requesting IP. A track resolved when it was queued may be dead by
  the time it plays, so every track carries an expiry and is re-resolved.
* **The output is not trusted.** The returned stream URL is checked before it
  reaches ffmpeg, and the metadata is sanitized before it reaches an API
  response or a Discord message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import time
from typing import Any

from .music import Track
from .net import UnsafeUrl, check_input_url, check_stream_url

log = logging.getLogger(__name__)

# yt-dlp's own messages are quoted back at operators in the log, never to
# callers, so these patterns only pick the class of failure worth explaining.
KNOWN_FAILURES: tuple[tuple[str, str], ...] = (
    ("private video", "That video is private."),
    ("members-only", "That video is members-only."),
    ("video unavailable", "That video is unavailable."),
    ("removed by the uploader", "That video was removed by its uploader."),
    ("age", "That video is age-restricted and cannot be played by a bot."),
    ("not available in your country", "That video is blocked in this server's region."),
    ("sign in to confirm", "YouTube is asking this host to sign in; it is rate limited."),
    ("unable to extract", "YouTube changed something; yt-dlp needs updating."),
    ("http error 429", "YouTube is rate limiting this host. Try again later."),
)

TITLE_MAX = 200
# Titles reach a JSON response and a Discord message. Control characters and
# bidi overrides are how a title lies about what it is.
# Code points, not literals: these characters are invisible, and a source
# file carrying them is the same trick this pattern exists to strip out.
UNSAFE_RANGES = (
    (0x00, 0x1F),  # C0 controls, newlines included
    (0x7F, 0x9F),  # DEL and C1 controls
    (0x200B, 0x200F),  # zero-width and directional marks
    (0x202A, 0x202E),  # bidi overrides - the trojan-source trick
    (0x2066, 0x2069),  # bidi isolates
    (0xFEFF, 0xFEFF),  # byte-order mark
)
UNSAFE_TITLE = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in UNSAFE_RANGES) + "]")


class TrackResolutionError(RuntimeError):
    """A link could not be turned into something playable. Becomes a 502."""


class SourceDisabled(RuntimeError):
    """The source exists but is switched off. Becomes a 503."""


def sanitize_title(raw: Any) -> str:
    title = UNSAFE_TITLE.sub("", str(raw or "")).strip()
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"
    return title or "Unknown track"


def classify(stderr: str) -> str:
    """Turn yt-dlp's output into something an operator can act on."""
    lowered = stderr.lower()
    for needle, message in KNOWN_FAILURES:
        if needle in lowered:
            return message
    return "Could not resolve that link."


async def run_ytdlp(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """Run yt-dlp as a child process, killing it if it overruns.

    Prefers the `yt-dlp` executable and falls back to `python -m yt_dlp`, so it
    works whether the optional requirement was installed into the image or into
    a virtualenv.
    """
    executable = shutil.which("yt-dlp")
    command = [executable, *args] if executable else [sys.executable, "-m", "yt_dlp", *args]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise TrackResolutionError(
            "yt-dlp is not installed in this image. Rebuild with "
            "--build-arg WITH_YTDLP=true, or set MUSIC_YTDLP_ENABLED=false."
        ) from exc

    try:
        stdout, stderr = await process.communicate()
    except (TimeoutError, asyncio.CancelledError):
        # The whole point of using a subprocess: this actually stops the work.
        # Cancellation counts too - the caller's deadline is enforced outside
        # this function, and a child left running would outlive it.
        process.kill()
        with _ignore():
            await process.wait()
        raise
    return process.returncode, stdout, stderr


class _ignore:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True


class YtDlpResolver:
    """Resolves YouTube links to playable stream URLs.

    Every failure leaves as TrackResolutionError. Nothing raised by yt-dlp, and
    no URL or path it mentions, reaches the caller.
    """

    name = "youtube"

    def __init__(self, config, runner=run_ytdlp, clock=time.monotonic) -> None:
        self.config = config
        self.enabled = bool(config.music_youtube_enabled)
        # Injected so tests need neither the package nor a network.
        self._runner = runner
        self._clock = clock
        self._semaphore = asyncio.Semaphore(max(1, config.music_ytdlp_max_concurrent))

    # -- plumbing ----------------------------------------------------------

    def _check_enabled(self) -> None:
        if not self.enabled:
            raise SourceDisabled("YouTube playback is turned off on this bot.")

    def _base_args(self) -> list[str]:
        return [
            "--dump-single-json",
            "--no-playlist",
            "--skip-download",
            "--no-warnings",
            "--no-progress",
            "--no-call-home",
            # Never let a link write to this disk; the recording owns it.
            "--no-cache-dir",
            "--socket-timeout",
            "10",
            "--retries",
            "2",
            "-f",
            "bestaudio[acodec!=none]/bestaudio/best",
        ]

    async def _extract(self, target: str, extra: list[str] | None = None) -> dict[str, Any]:
        self._check_enabled()
        args = [*self._base_args(), *(extra or []), "--", target]
        timeout = self.config.music_resolve_timeout_seconds
        async with self._semaphore:
            try:
                # One authoritative deadline, enforced here rather than inside
                # the runner, so it holds for any runner and nothing keeps the
                # semaphore past it. run_ytdlp kills its child on cancellation.
                code, stdout, stderr = await asyncio.wait_for(
                    self._runner(args, timeout), timeout=timeout
                )
            except TimeoutError:
                log.warning("yt-dlp timed out resolving %r", target)
                raise
            except TrackResolutionError:
                raise
            except Exception as exc:  # noqa: BLE001 - one shape for the caller
                log.exception("yt-dlp could not be run")
                raise TrackResolutionError("Could not run the link resolver.") from exc

        if code != 0:
            message = stderr.decode("utf-8", "replace")
            log.warning("yt-dlp failed (%s) for %r: %s", code, target, message.strip()[:500])
            raise TrackResolutionError(classify(message))

        try:
            return json.loads(stdout.decode("utf-8", "replace"))
        except ValueError as exc:
            log.warning("yt-dlp returned output that is not JSON for %r", target)
            raise TrackResolutionError("The link resolver returned nothing usable.") from exc

    # -- guards ------------------------------------------------------------

    def _check_playable(self, info: dict[str, Any]) -> None:
        if info.get("is_live") and not self.config.music_allow_live:
            raise TrackResolutionError(
                "That is a live stream. Anything queued behind it would never play."
            )
        duration = info.get("duration")
        if duration and duration > self.config.music_max_track_seconds:
            hours = self.config.music_max_track_seconds / 3600
            raise TrackResolutionError(f"That track is longer than the {hours:.0f} hour limit.")

    def _stream_url(self, info: dict[str, Any]) -> str:
        url = info.get("url")
        if not url:
            # A manifest-only result (DASH/HLS split into formats) still has one.
            for candidate in info.get("formats") or []:
                if candidate.get("acodec") not in (None, "none") and candidate.get("url"):
                    url = candidate["url"]
                    break
        if not url:
            raise TrackResolutionError("That link has no playable audio stream.")
        try:
            # The extractor's output is not trusted: this is what stops a
            # resolved URL pointing back inside the network.
            return check_stream_url(url)
        except UnsafeUrl as exc:
            log.error("yt-dlp returned an unsafe stream URL: %s", exc)
            raise TrackResolutionError("The resolved stream is not safe to play.") from exc

    def _to_track(self, info: dict[str, Any], *, with_stream: bool) -> Track:
        page = info.get("webpage_url") or info.get("original_url") or ""
        duration = info.get("duration")
        track = Track(
            id=page or info.get("id", ""),
            title=sanitize_title(info.get("title")),
            source=self.name,
            uri=self._stream_url(info) if with_stream else "",
            duration_seconds=float(duration) if duration else None,
        )
        if with_stream:
            track.expires_at = self._clock() + self._ttl_for(info)
        return track

    def _ttl_for(self, info: dict[str, Any]) -> float:
        """How long the stream URL can be trusted.

        YouTube puts an absolute `expire` timestamp in the query string. When it
        is there it is authoritative; otherwise fall back to the configured TTL.
        Either way a margin is taken off, because a URL that expires while
        ffmpeg is connecting fails just as hard as one that expired an hour ago.
        """
        configured = self.config.music_stream_ttl_seconds
        match = re.search(r"[?&]expire=(\d+)", info.get("url") or "")
        if match:
            remaining = int(match.group(1)) - time.time()
            if remaining > 0:
                return max(60.0, min(configured, remaining - 120))
        return configured

    # -- the interface tracks.py exposes -----------------------------------

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        """Search by words, or resolve a pasted link.

        Search results deliberately carry no stream URL: resolving every hit
        would be several times the work for results nobody plays, and the URL
        would be stale by the time one was clicked anyway.
        """
        self._check_enabled()
        if query.startswith(("http://", "https://")):
            return [await self.resolve(query)]

        count = max(1, min(limit, 10))
        info = await self._extract(f"ytsearch{count}:{query}", extra=["--flat-playlist"])
        entries = info.get("entries") or []
        tracks = []
        for entry in entries:
            if not entry:
                continue
            with _ignore():
                tracks.append(self._to_track(entry, with_stream=False))
        return tracks

    async def resolve(self, track_id: str) -> Track:
        """Turn a link into a track with a live, checked stream URL."""
        self._check_enabled()
        try:
            target = check_input_url(track_id)
        except UnsafeUrl as exc:
            raise TrackResolutionError(str(exc)) from exc

        info = await self._extract(target)
        self._check_playable(info)
        track = self._to_track(info, with_stream=True)
        log.info("Resolved %s (%.0fs)", track.title, track.duration_seconds or 0)
        return track

    # `browse` keeps the TrackSource shape so the routes can stay uniform.
    async def browse(self, query: str | None = None, limit: int = 5) -> list[Track]:
        self._check_enabled()
        if not query:
            return []
        return await self.search(query, limit)
