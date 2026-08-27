"""Nightly SQLite backup.

Uses the SQLite online backup API rather than a raw file copy, so a backup taken
while the bot is mid-write is still consistent. This is a cheap safety net for
the metadata only - it does not replace a host-level backup of /data.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_BACKUP_RE = re.compile(r"^bot-(\d{4}-\d{2}-\d{2})\.db$")


def backup_filename(day: date) -> str:
    return f"bot-{day.isoformat()}.db"


def backup_database(db_path: Path, backups_dir: Path, day: date | None = None) -> Path:
    day = day or datetime.now().date()
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / backup_filename(day)
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    log.info("Database backed up to %s", target)
    return target


def prune_targets(names: Sequence[str], keep_days: int, today: date) -> list[str]:
    """Backup filenames older than the retention window."""
    cutoff = today - timedelta(days=keep_days)
    stale: list[str] = []
    for name in names:
        match = _BACKUP_RE.match(name)
        if not match:
            continue
        if date.fromisoformat(match.group(1)) < cutoff:
            stale.append(name)
    return sorted(stale)


def prune_backups(backups_dir: Path, keep_days: int, today: date | None = None) -> list[str]:
    today = today or datetime.now().date()
    if not backups_dir.is_dir():
        return []
    names = [p.name for p in backups_dir.iterdir() if p.is_file()]
    removed = prune_targets(names, keep_days, today)
    for name in removed:
        (backups_dir / name).unlink(missing_ok=True)
        log.info("Pruned old backup %s", name)
    return removed
