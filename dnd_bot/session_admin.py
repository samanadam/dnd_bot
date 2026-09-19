"""Removing a session for good, and everything the bot keeps about it.

Order matters. Files and remote copies go first and the database row goes last,
so a failure part-way leaves a session that still shows in the list and can be
deleted again, rather than files nobody can see or reach. Every step tolerates
its target already being gone, which is what makes that retry safe.

The transcriber keeps its own archive on its own machine; that is not reachable
from here and is not touched.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import outbox, paths
from .r2 import INBOX_PREFIX, OUTBOX_PREFIX

log = logging.getLogger(__name__)

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NAME_MAX = 100


class SessionBusy(RuntimeError):
    """The session cannot be removed right now; the message is safe to show."""


@dataclass(frozen=True)
class Removed:
    files: int
    bytes_freed: int
    remote_objects: int


def normalize_title(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("name must be a string.")
    name = raw.strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise ValueError(f"name must be 1-{NAME_MAX} characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("name must not contain control characters.")
    return name


def _remove_tree(target: Path, root: Path) -> tuple[int, int]:
    """Delete one directory that must sit directly under `root`. Returns (files, bytes)."""
    if not target.exists() and not target.is_symlink():
        return 0, 0
    if target.is_symlink() or target.resolve().parent != root.resolve():
        # Never follow a link out of the data directory.
        raise SessionBusy("Refusing to delete a path outside the data directory.")
    files = [p for p in target.rglob("*") if p.is_file() and not p.is_symlink()]
    size = sum(p.stat().st_size for p in files)
    shutil.rmtree(target)
    return len(files), size


def _remove_file(target: Path, root: Path) -> tuple[int, int]:
    if not target.is_file() and not target.is_symlink():
        return 0, 0
    if target.is_symlink() or target.resolve().parent != root.resolve():
        raise SessionBusy("Refusing to delete a path outside the data directory.")
    size = target.stat().st_size
    target.unlink()
    return 1, size


def remove_files(config: Any, store: Any, session_id: str) -> Removed:
    """Blocking. Local directories, the export bundle, and any R2 copies."""
    if not SESSION_ID.match(session_id):
        raise ValueError("invalid session id")
    files = freed = 0
    for root, target in (
        (config.sessions_dir, paths.session_dir(config.sessions_dir, session_id)),
        (config.inbox_dir, config.inbox_dir / session_id),
        (config.outbox_dir, outbox.outbox_dir(config.outbox_dir, session_id)),
    ):
        count, size = _remove_tree(target, root)
        files += count
        freed += size
    count, size = _remove_file(
        paths.export_path(config.exports_dir, session_id), config.exports_dir
    )
    files += count
    freed += size

    remote = 0
    if store is not None:
        # The audio may still be in the bucket waiting for the transcriber, and a
        # finished transcript may be waiting to come back. Neither is wanted now.
        remote += store.delete_session(OUTBOX_PREFIX, session_id)
        remote += store.delete_session(INBOX_PREFIX, session_id)
    log.info(
        "Removed session %s: %s file(s), %.1f MB, %s remote object(s)",
        session_id,
        files,
        freed / 1_000_000,
        remote,
    )
    return Removed(files=files, bytes_freed=freed, remote_objects=remote)
