"""Where a playable track comes from.

Two backends, deliberately kept apart. R2 is the one the bot is built around:
files the table owns, listed and cached. YouTube lives in `ytdlp.py`, is
optional and off by default, and is wrapped so that its failures - which are
frequent, because YouTube changes and yt-dlp chases it - can never reach the R2
path or the recorder.

Callers pick a backend by name. Nothing here sniffs an id to decide, so an
outage in one source cannot change how the other behaves.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from .music import Track
from .musiccache import cached_path, ensure_room, prune
from .ytdlp import SourceDisabled, TrackResolutionError, YtDlpResolver

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".opus", ".ogg", ".mp3", ".m4a", ".flac", ".wav", ".aac"}


class TrackSource(Protocol):
    name: str
    enabled: bool

    async def browse(self, query: str | None, limit: int) -> list[Track]: ...

    async def resolve(self, track_id: str) -> Track: ...


class R2TrackSource:
    """Tracks the table owns, kept under a prefix in the existing bucket."""

    name = "r2"

    def __init__(self, store, config, ttl_seconds: float = 30.0) -> None:
        self.store = store
        self.config = config
        self.enabled = store is not None
        self._ttl = ttl_seconds
        self._cache: dict[str, int] = {}
        self._cached_at = 0.0

    @property
    def prefix(self) -> str:
        return f"{self.config.music_r2_prefix}/"

    async def _listing(self, force: bool = False) -> dict[str, int]:
        """Key -> size under the music prefix, cached briefly.

        The portal's track list is a keystroke away from being re-fetched on
        every render; a short TTL keeps that off R2 without anyone noticing a
        newly uploaded file is late by half a minute.
        """
        if not self.enabled:
            raise SourceDisabled("Object storage is not configured.")
        now = asyncio.get_running_loop().time()
        if force or not self._cache or now - self._cached_at > self._ttl:
            try:
                self._cache = await asyncio.to_thread(self.store.list_sizes, self.prefix)
            except Exception as exc:  # noqa: BLE001 - one shape for the caller
                raise TrackResolutionError("Could not list the music bucket.") from exc
            self._cached_at = now
        return self._cache

    def _track(self, key: str) -> Track:
        name = key[len(self.prefix) :]
        return Track(id=key, title=Path(name).stem.replace("_", " "), source=self.name, uri="")

    async def browse(self, query: str | None = None, limit: int = 50) -> list[Track]:
        listing = await self._listing()
        tracks = [
            self._track(key)
            for key in sorted(listing)
            if key != self.prefix and Path(key).suffix.lower() in AUDIO_SUFFIXES
        ]
        if query:
            needle = query.casefold()
            tracks = [t for t in tracks if needle in t.title.casefold()]
        return tracks[:limit]

    async def resolve(self, track_id: str) -> Track:
        """Download to the cache and hand back a local path.

        The id is only accepted if it is in the live listing. That is an
        allowlist rather than path arithmetic: there is no traversal to get
        wrong, and a key outside the prefix simply is not there.
        """
        listing = await self._listing()
        if track_id not in listing:
            await self._listing(force=True)
            if track_id not in self._cache:
                raise TrackResolutionError("No such track in the music bucket.")

        target = cached_path(self.config.music_cache_dir, track_id)
        if target.exists() and target.stat().st_size == listing.get(track_id, -1):
            return self._playable(track_id, target)

        ensure_room(
            self.config.music_cache_dir,
            self.config.data_dir,
            self.config.disk_warning_threshold_mb,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(
                self.store.client.download_file,
                Bucket=self.store.bucket,
                Key=track_id,
                Filename=str(target),
            )
        except Exception as exc:  # noqa: BLE001 - one shape for the caller
            raise TrackResolutionError("Could not download that track.") from exc

        await asyncio.to_thread(prune, self.config.music_cache_dir, self.config.music_cache_max_mb)
        return self._playable(track_id, target)

    def _playable(self, key: str, path: Path) -> Track:
        track = self._track(key)
        track.uri = str(path)
        return track


def build_sources(config, store) -> dict[str, TrackSource]:
    """Every source this configuration can offer, keyed by name."""
    sources: dict[str, TrackSource] = {}
    if store is not None:
        sources["r2"] = R2TrackSource(store, config)
    if config.music_youtube_enabled:
        sources["youtube"] = YtDlpResolver(config)
    return sources
