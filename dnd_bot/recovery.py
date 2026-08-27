"""Crash recovery.

A session row stays `completed = 0` until it is properly stopped. On startup we
list every such row that still has raw audio on disk so a human can finish it
with `/session recover <id>`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .finalize import has_raw_audio

log = logging.getLogger(__name__)


def find_recoverable(
    rows: Sequence[dict[str, Any]],
    has_audio: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """Incomplete, non-cancelled sessions that still have audio worth saving."""
    recoverable: list[dict[str, Any]] = []
    for row in rows:
        if row.get("completed"):
            continue
        if row.get("cancelled"):
            continue
        if not has_audio(row["id"]):
            continue
        recoverable.append(row)
    return recoverable


async def scan_for_recoverable(db, sessions_root: Path) -> list[dict[str, Any]]:
    rows = await db.list_open_sessions()
    recoverable = find_recoverable(rows, lambda sid: has_raw_audio(sessions_root, sid))
    for row in recoverable:
        log.warning(
            "Recoverable session %s (%s in #%s) - run /session recover %s",
            row["id"],
            row.get("name") or "unnamed",
            row.get("channel_name"),
            row["id"],
        )
    if not recoverable:
        log.info("No recoverable sessions found on startup")
    return recoverable
