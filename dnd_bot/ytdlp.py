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
import contextlib
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .music import Track
from .musiccache import CacheFull, ensure_room, prune
from .net import YOUTUBE_ID, UnsafeUrl, canonical_watch_url, check_input_url, check_stream_url

log = logging.getLogger(__name__)

# yt-dlp's own messages are quoted back at operators in the log, never to
# callers, so these patterns only pick the class of failure worth explaining.
KNOWN_FAILURES: tuple[tuple[str, str], ...] = (
    ("private video", "That video is private."),
    ("members-only", "That video is members-only."),
    ("video unavailable", "That video is unavailable."),
    ("this video is unavailable", "That video is unavailable."),
    ("removed by the uploader", "That video was removed by its uploader."),
    ("age", "That video is age-restricted and cannot be played by a bot."),
    ("not available in your country", "That video is blocked in this server's region."),
    ("sign in to confirm", "YouTube is asking this host to sign in; it is rate limited."),
    ("unable to extract", "YouTube changed something; yt-dlp needs updating."),
    ("http error 429", "YouTube is rate limiting this host. Try again later."),
)

TITLE_MAX = 200

# Soundboard sounds from YouTube live here, inside the music cache, so the same
# disk guard and size cap apply to them as to a track pulled from the bucket.
LAYER_DIR = "youtube"
# Only formats ffmpeg reads from disk without help; anything else is refused.
LAYER_SUFFIXES = (".m4a", ".webm", ".opus", ".ogg", ".mp3")
# Ambience and effects take exactly this link form: nothing to normalise, and
# the video id is known before any network call.
LAYER_LINK = re.compile(r"https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})")
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

    def __init__(self, config, runner=run_ytdlp, clock=time.monotonic, prober=None) -> None:
        self.config = config
        self.enabled = bool(config.music_youtube_enabled)
        # Injected so tests need neither the package nor a network.
        self._runner = runner
        self._clock = clock
        self._prober = prober
        self._semaphore = asyncio.Semaphore(max(1, config.music_ytdlp_max_concurrent))
        # One fetch per video at a time: two downloads into one file name would
        # corrupt each other. [lock, users] so the entry goes when nobody waits.
        self._video_locks: dict[str, list[Any]] = {}

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
        video_id = info.get("id", "")
        if isinstance(video_id, str) and YOUTUBE_ID.fullmatch(video_id):
            # A search hit carries only the bare id, and resolve() wants a link.
            # One canonical form also lets the same video be recognised wherever
            # it turns up (queue, saved list, soundboard layer).
            page = canonical_watch_url(video_id)
        else:
            page = info.get("webpage_url") or info.get("original_url") or ""
        duration = info.get("duration")
        track = Track(
            id=page or video_id,
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

    # -- soundboard sounds: downloaded once, played from disk --------------

    @property
    def layer_dir(self) -> Path:
        return Path(self.config.music_cache_dir) / LAYER_DIR

    def _layer_limit(self, kind: str) -> float:
        if kind == "sfx":
            return float(self.config.music_yt_sfx_max_seconds)
        if kind == "ambience":
            return float(self.config.music_yt_ambience_max_seconds)
        raise TrackResolutionError("kind must be ambience or sfx.")

    @staticmethod
    def _describe_limit(seconds: float) -> str:
        return f"{seconds / 60:.0f} minute" if seconds >= 120 else f"{seconds:.0f} second"

    def _layer_files(self, video_id: str) -> list[Path]:
        # The id was matched against a strict pattern, so it holds no glob syntax.
        if not self.layer_dir.is_dir():
            return []
        return sorted(self.layer_dir.glob(f"yt_{video_id}.*"))

    def _audio_file(self, video_id: str) -> Path | None:
        for path in self._layer_files(video_id):
            if path.suffix.lower() in LAYER_SUFFIXES and path.is_file():
                return path
        return None

    def _discard(self, video_id: str) -> None:
        """Remove everything a video left behind, partial downloads included."""
        for path in self._layer_files(video_id):
            with contextlib.suppress(OSError):
                path.unlink()

    def _sidecar(self, video_id: str) -> Path:
        return self.layer_dir / f"yt_{video_id}.json"

    def _read_sidecar(self, video_id: str) -> tuple[str, float] | None:
        """Title and length saved beside a download, or None if unusable."""
        try:
            data = json.loads(self._sidecar(video_id).read_text(encoding="utf-8"))
            title, duration = data["title"], data["duration"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(title, str) or isinstance(duration, bool):
            return None
        if not isinstance(duration, int | float) or not math.isfinite(duration) or duration <= 0:
            return None
        return sanitize_title(title), float(duration)

    def _write_sidecar(self, video_id: str, title: str, duration: float) -> None:
        target = self._sidecar(video_id)
        scratch = target.with_name(target.name + ".tmp")
        scratch.write_text(json.dumps({"title": title, "duration": duration}), encoding="utf-8")
        os.replace(scratch, target)

    def _check_layer(self, info: dict[str, Any], kind: str, video_id: str) -> None:
        """Refuse what should not be downloaded, before downloading it."""
        if info.get("_type") == "playlist" or info.get("entries"):
            raise TrackResolutionError("That link is a playlist. Use a single video.")
        if info.get("id") != video_id:
            raise TrackResolutionError("That link did not resolve to the video it names.")
        if info.get("is_live"):
            raise TrackResolutionError("That is a live stream and cannot be saved as a sound.")
        duration = info.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, int | float) or duration <= 0:
            raise TrackResolutionError("That video has no known length.")
        self._check_layer_length(float(duration), kind)

    def _check_layer_length(self, duration: float, kind: str) -> None:
        limit = self._layer_limit(kind)
        if duration > limit:
            noun = "effects" if kind == "sfx" else "ambience"
            raise TrackResolutionError(
                f"That is longer than the {self._describe_limit(limit)} limit for {noun}."
            )

    def _layer_args(self, video_id: str) -> list[str]:
        # `%` is yt-dlp's template escape; a data directory containing one must
        # not be read as a field.
        template = str(self.layer_dir).replace("%", "%%") + os.sep + f"yt_{video_id}.%(ext)s"
        return [
            # A stray config file must not be able to change what is written where.
            "--ignore-config",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--no-cache-dir",
            "--no-continue",
            # The cache prunes by modification time; the upload date would make
            # every fresh download the first to go.
            "--no-mtime",
            "--socket-timeout",
            "10",
            "--retries",
            "2",
            "--max-filesize",
            f"{max(1, int(self.config.music_yt_layer_max_mb))}M",
            "-f",
            "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "-o",
            template,
            "--",
            canonical_watch_url(video_id),
        ]

    @contextlib.asynccontextmanager
    async def _video_lock(self, video_id: str):
        entry = self._video_locks.setdefault(video_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._video_locks.pop(video_id, None)

    def _layer_track(self, video_id: str, path: Path, title: str, duration: float) -> Track:
        return Track(
            id=canonical_watch_url(video_id),
            title=title,
            source=self.name,
            uri=str(path),
            duration_seconds=round(duration, 1),
        )

    async def fetch_layer(self, track_id: str, kind: str) -> Track:
        """A YouTube video as a local audio file, ready for the soundboard.

        The link only names a video. Everything after that is built from its
        11-character id: the URL yt-dlp fetches and the file name.
        """
        self._check_enabled()
        limit_seconds = self._layer_limit(kind)  # also rejects an unknown kind
        try:
            check_input_url(track_id)
        except UnsafeUrl as exc:
            raise TrackResolutionError(str(exc)) from exc
        match = LAYER_LINK.fullmatch(track_id)
        if match is None:
            raise TrackResolutionError("Ambience and effects need a plain YouTube video link.")
        video_id = match.group(1)

        async with self._video_lock(video_id):
            hit = self._cached_layer(video_id, kind)
            if hit is not None:
                return hit
            return await self._download_layer(video_id, kind, limit_seconds)

    def _cached_layer(self, video_id: str, kind: str) -> Track | None:
        path = self._audio_file(video_id)
        saved = self._read_sidecar(video_id) if path is not None else None
        if path is None or saved is None:
            return None
        title, duration = saved
        # Saved as an effect and asked for as ambience is fine; the other way
        # round meets the same length check a fresh link gets.
        self._check_layer_length(duration, kind)
        # Both files count as used, so the cache's oldest-first pruning keeps
        # the sounds the table actually reaches for.
        for used in (path, self._sidecar(video_id)):
            with contextlib.suppress(OSError):
                os.utime(used)
        return self._layer_track(video_id, path, title, duration)

    async def _download_layer(self, video_id: str, kind: str, limit_seconds: float) -> Track:
        info = await self._extract(canonical_watch_url(video_id))
        self._check_layer(info, kind, video_id)
        title = sanitize_title(info.get("title"))

        try:
            ensure_room(
                self.config.music_cache_dir,
                self.config.data_dir,
                self.config.disk_warning_threshold_mb,
            )
        except CacheFull as exc:
            raise TrackResolutionError("Not enough free disk to save that sound now.") from exc

        self.layer_dir.mkdir(parents=True, exist_ok=True)
        self._discard(video_id)
        timeout = float(self.config.music_yt_download_timeout_seconds)
        try:
            async with self._semaphore:
                code, _stdout, stderr = await asyncio.wait_for(
                    self._runner(self._layer_args(video_id), timeout), timeout=timeout
                )
        except TimeoutError:
            log.warning("yt-dlp timed out downloading %s", video_id)
            self._discard(video_id)
            raise
        except TrackResolutionError:
            self._discard(video_id)
            raise
        except Exception as exc:  # noqa: BLE001 - one shape for the caller
            log.exception("yt-dlp could not be run")
            self._discard(video_id)
            raise TrackResolutionError("Could not run the link resolver.") from exc

        if code != 0:
            message = stderr.decode("utf-8", "replace")
            log.warning(
                "yt-dlp failed (%s) downloading %s: %s", code, video_id, message.strip()[:500]
            )
            self._discard(video_id)
            raise TrackResolutionError(classify(message))

        limit_mb = max(1, int(self.config.music_yt_layer_max_mb))
        path = self._audio_file(video_id)
        if path is None:
            self._discard(video_id)
            raise TrackResolutionError(
                f"That audio is larger than the {limit_mb} MB limit or could not be saved."
            )
        if path.stat().st_size > limit_mb * 1_000_000:
            self._discard(video_id)
            raise TrackResolutionError(f"That audio is larger than the {limit_mb} MB limit.")

        prober = self._prober
        if prober is None:
            from .uploads import probe_duration as prober
        try:
            duration = await asyncio.to_thread(prober, path)
        except Exception:  # noqa: BLE001 - treated like a file with no audio
            duration = None
        # ffprobe, not the extractor, has the last word on what the file is.
        if not duration or duration > limit_seconds + 5:
            self._discard(video_id)
            raise TrackResolutionError("That download is not playable audio of the right length.")

        self._write_sidecar(video_id, title, float(duration))
        await asyncio.to_thread(prune, self.config.music_cache_dir, self.config.music_cache_max_mb)
        log.info("Saved %s (%.0fs) for the soundboard", title, duration)
        return self._layer_track(video_id, path, title, float(duration))
