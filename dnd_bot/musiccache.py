"""On-disk cache for downloaded tracks.

It shares a disk with raw capture, which runs at roughly 0.7 GB per speaker
hour and cannot pause to wait for space. So the cache always loses: it refuses
to grow when the disk is already near the warning threshold, and it prunes
itself back to a hard cap after every fetch.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .cleanup import directory_size_bytes, free_space_mb

log = logging.getLogger(__name__)


class CacheFull(RuntimeError):
    """The disk is too tight to cache anything more right now."""


def ensure_room(cache_dir: Path, data_dir: Path, threshold_mb: int) -> None:
    """Refuse to cache when a session would be the thing that runs out of disk."""
    free = free_space_mb(data_dir)
    if free < threshold_mb:
        raise CacheFull(
            f"Only {free:.0f} MB free; not caching music below the "
            f"{threshold_mb} MB disk warning threshold."
        )


def prune(cache_dir: Path, max_mb: int) -> int:
    """Evict least-recently-used files until the cache fits. Returns bytes freed.

    Access time is unreliable on a lot of filesystems (relatime, noatime), so
    this uses mtime: a re-download refreshes it, which is close enough to LRU
    for a folder of ambient tracks.
    """
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return 0
    limit = max_mb * 1_000_000
    total = directory_size_bytes(cache_dir)
    if total <= limit:
        return 0

    files = sorted(
        (p for p in cache_dir.rglob("*") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    freed = 0
    for path in files:
        if total - freed <= limit:
            break
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError:
            log.warning("Could not evict %s from the music cache", path.name)
            continue
        freed += size
        log.info("Evicted %s from the music cache (%.1f MB)", path.name, size / 1_000_000)
    return freed


def cached_path(cache_dir: Path, key: str) -> Path:
    """Where one object key lands locally.

    The key is flattened rather than mirrored as a directory tree: the name is
    only ever produced from a key that was already matched against a live
    listing, and a flat folder is one less traversal surface to reason about.
    """
    return Path(cache_dir) / key.replace("/", "_")
