"""Bulk-importing a local folder of ambience and effects into the music bucket.

The soundboard library lives in R2 and nowhere else: this reads a folder once,
puts each sound in the bucket and keeps no copy. Every file goes through the same
checks as an upload from the portal, in the same order, and nothing that is
already in the bucket is overwritten:

    <folder>/ambience/*.ogg  ->  <prefix>/ambience/
    <folder>/sfx/*.wav       ->  <prefix>/sfx/

Only those two folders are read, and only their direct children.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .uploads import (
    CONTENT_TYPES,
    MAX_SFX_SECONDS,
    SOUNDBOARD_FOLDERS,
    Probe,
    UploadError,
    looks_like,
    object_key,
    probe_audio,
)

log = logging.getLogger(__name__)


class NothingToImport(Exception):
    """The folder has neither an ambience nor an sfx sub-folder."""


@dataclass(frozen=True)
class Outcome:
    path: Path
    folder: str
    status: str  # uploaded | would upload | skipped | failed
    reason: str = ""
    key: str | None = None


def import_sounds(
    root: Path,
    store,  # noqa: ANN001 - R2Store
    *,
    prefix: str,
    max_bytes: int,
    max_ambience_seconds: float,
    prober: Callable[[Path], Probe] = probe_audio,
    dry_run: bool = False,
) -> list[Outcome]:
    """Import every sound under `root/ambience` and `root/sfx`.

    A dry run performs every check, including the ffprobe, and uploads nothing.
    """
    root = Path(root)
    folders = [folder for folder in SOUNDBOARD_FOLDERS if (root / folder).is_dir()]
    if not folders:
        raise NothingToImport(f"{root} has no ambience/ or sfx/ folder.")

    taken = set(store.list_keys(f"{prefix}/"))
    outcomes: list[Outcome] = []
    for folder in folders:
        limit = MAX_SFX_SECONDS if folder == "sfx" else max_ambience_seconds
        files = sorted(
            path for path in (root / folder).iterdir() if path.is_file() and path.name[0] != "."
        )
        for path in files:
            outcome = _import_one(
                path, folder, store, prefix, taken, max_bytes, limit, prober, dry_run
            )
            if outcome.key is not None and outcome.status != "skipped":
                taken.add(outcome.key)
            outcomes.append(outcome)
    return outcomes


def _import_one(
    path: Path,
    folder: str,
    store,  # noqa: ANN001 - R2Store
    prefix: str,
    taken: set[str],
    max_bytes: int,
    limit: float,
    prober: Callable[[Path], Probe],
    dry_run: bool,
) -> Outcome:
    def skip(reason: str, key: str | None = None) -> Outcome:
        return Outcome(path, folder, "skipped", reason, key)

    try:
        key = object_key(prefix, folder, path.name)
    except UploadError as exc:
        return skip(exc.message)
    if key in taken:
        return skip("A sound with that name is already in the bucket or earlier in this import.")

    size = path.stat().st_size
    if size == 0:
        return skip("The file is empty.")
    if size > max_bytes:
        return skip(f"The file is over the {max_bytes // 1_000_000} MB size limit.")

    suffix = Path(key).suffix.lower()
    with path.open("rb") as handle:
        head = handle.read(16)
    if not looks_like(head, suffix):
        return skip(f"Not a valid {suffix[1:]} file.")

    probe = prober(path)
    if not probe.has_audio or not probe.duration_seconds:
        return skip("No playable audio was found in the file.")
    if probe.duration_seconds > limit:
        return skip(f"Too long for {folder} ({int(limit)} s at most).")

    if dry_run:
        return Outcome(path, folder, "would upload", key=key)
    try:
        store.client.upload_file(
            Filename=str(path),
            Bucket=store.bucket,
            Key=key,
            ExtraArgs={"ContentType": CONTENT_TYPES.get(suffix, "application/octet-stream")},
        )
    except Exception as exc:  # noqa: BLE001 - one bad upload must not stop the batch
        log.debug("Upload of %s failed", key, exc_info=True)
        return Outcome(path, folder, "failed", f"{type(exc).__name__}: {exc}", key)
    return Outcome(path, folder, "uploaded", key=key)
