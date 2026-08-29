"""SQLite access layer.

All structured state lives here rather than in flat files so concurrent sessions
in different voice channels cannot race on shared writes. Audio blobs stay on
the filesystem; this database only holds metadata.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .timeutil import to_iso, utcnow

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def discover_migrations(directory: Path) -> list[Migration]:
    """Return migrations sorted by numeric version prefix."""
    migrations: list[Migration] = []
    if not directory.is_dir():
        return migrations
    for path in sorted(directory.iterdir()):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            continue
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=path.name,
                sql=path.read_text(encoding="utf-8"),
            )
        )
    migrations.sort(key=lambda m: m.version)
    return migrations


class Database:
    """Thin async wrapper around a single SQLite file."""

    def __init__(self, path: Path, migrations_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.migrations_dir = migrations_dir or MIGRATIONS_DIR
        self._conn: aiosqlite.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def migrate(self) -> list[int]:
        """Apply any migrations newer than the recorded schema version."""
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
        )
        await self.conn.commit()
        cursor = await self.conn.execute("SELECT version FROM schema_version")
        applied = {row["version"] for row in await cursor.fetchall()}
        newly_applied: list[int] = []
        for migration in discover_migrations(self.migrations_dir):
            if migration.version in applied:
                continue
            await self.conn.executescript(migration.sql)
            await self.conn.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, to_iso(utcnow())),
            )
            await self.conn.commit()
            newly_applied.append(migration.version)
            log.info("Applied migration %s", migration.name)
        return newly_applied

    # -- sessions ----------------------------------------------------------

    async def create_session(
        self,
        *,
        session_id: str,
        name: str | None,
        guild_id: int,
        channel_id: int,
        channel_name: str,
        text_channel_id: int | None,
        started_by_user_id: int,
        start_time: str,
        participants: dict[str, str],
        language: str,
        model_used: str,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO sessions (
                id, name, guild_id, channel_id, channel_name, text_channel_id,
                started_by_user_id, start_time, participants_json, language, model_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                name,
                str(guild_id),
                str(channel_id),
                channel_name,
                str(text_channel_id) if text_channel_id else None,
                str(started_by_user_id),
                start_time,
                json.dumps(participants, ensure_ascii=False),
                language,
                model_used,
            ),
        )
        await self.conn.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cursor = await self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self.conn.execute(
            f"UPDATE sessions SET {assignments} WHERE id = ?",
            (*fields.values(), session_id),
        )
        await self.conn.commit()

    async def merge_participants(self, session_id: str, participants: dict[str, str]) -> None:
        """Add newly-seen speakers without dropping the ones already recorded."""
        row = await self.get_session(session_id)
        if row is None:
            return
        current = json.loads(row["participants_json"] or "{}")
        current.update(participants)
        await self.update_session(
            session_id, participants_json=json.dumps(current, ensure_ascii=False)
        )

    async def set_offsets(self, session_id: str, offsets: dict[str, float]) -> None:
        await self.update_session(session_id, offsets_json=json.dumps(offsets, ensure_ascii=False))

    async def list_sessions(self, limit: int = 25) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE completed = 1 AND cancelled = 0 "
            "ORDER BY start_time DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def list_open_sessions(self) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE completed = 0 AND cancelled = 0 ORDER BY start_time"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def list_sessions_with_audio(self) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE audio_expires_at IS NOT NULL"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def active_session_in_channel(
        self, guild_id: int, channel_id: int
    ) -> dict[str, Any] | None:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND channel_id = ? "
            "AND completed = 0 AND cancelled = 0",
            (str(guild_id), str(channel_id)),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_session(self, session_id: str) -> None:
        await self.conn.execute(
            "DELETE FROM transcription_queue WHERE session_id = ?", (session_id,)
        )
        await self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self.conn.commit()

    # -- characters --------------------------------------------------------

    async def set_character(self, user_id: int, character_name: str) -> None:
        await self.conn.execute(
            "INSERT INTO characters (user_id, character_name, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET character_name = excluded.character_name, "
            "updated_at = excluded.updated_at",
            (str(user_id), character_name, to_iso(utcnow())),
        )
        await self.conn.commit()

    async def clear_character(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM characters WHERE user_id = ?", (str(user_id),))
        await self.conn.commit()

    async def character_map(self) -> dict[str, str]:
        cursor = await self.conn.execute("SELECT user_id, character_name FROM characters")
        return {row["user_id"]: row["character_name"] for row in await cursor.fetchall()}

    # -- transcription queue ----------------------------------------------

    async def mark_exported(self, session_id: str) -> int | None:
        """Record that a session is staged and waiting for the transcriber.

        The table was a work queue when transcription happened in-process. It
        now tracks where a session has got to in the handover: exported ->
        transcribing -> done.
        """
        cursor = await self.conn.execute(
            "SELECT id FROM transcription_queue WHERE session_id = ? AND status != 'done'",
            (session_id,),
        )
        if await cursor.fetchone():
            return None
        cursor = await self.conn.execute(
            "INSERT INTO transcription_queue (session_id, queued_at, status) VALUES (?, ?, ?)",
            (session_id, to_iso(utcnow()), "exported"),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def mark_session_state(self, session_id: str, status: str, **fields: Any) -> None:
        """Move a session along the handover, creating the row if it is missing."""
        payload: dict[str, Any] = {"status": status, **fields}
        assignments = ", ".join(f"{key} = ?" for key in payload)
        cursor = await self.conn.execute(
            f"UPDATE transcription_queue SET {assignments} WHERE session_id = ?",
            (*payload.values(), session_id),
        )
        if not cursor.rowcount:
            await self.conn.execute(
                "INSERT INTO transcription_queue (session_id, queued_at, status) VALUES (?, ?, ?)",
                (session_id, to_iso(utcnow()), status),
            )
        await self.conn.commit()

    async def session_state(self, session_id: str) -> str | None:
        cursor = await self.conn.execute(
            "SELECT status FROM transcription_queue WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row["status"] if row else None

    async def awaiting_transcription(self) -> list[dict[str, Any]]:
        """Sessions staged for the transcriber that have not come back yet."""
        cursor = await self.conn.execute(
            "SELECT q.session_id, q.queued_at, q.status, s.name FROM transcription_queue q "
            "LEFT JOIN sessions s ON s.id = q.session_id "
            "WHERE q.status != 'done' ORDER BY q.queued_at"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def mark_job(self, job_id: int, status: str, **fields: Any) -> None:
        payload: dict[str, Any] = {"status": status, **fields}
        assignments = ", ".join(f"{key} = ?" for key in payload)
        await self.conn.execute(
            f"UPDATE transcription_queue SET {assignments} WHERE id = ?",
            (*payload.values(), job_id),
        )
        await self.conn.commit()

    async def increment_attempts(self, job_id: int) -> None:
        await self.conn.execute(
            "UPDATE transcription_queue SET attempts = attempts + 1 WHERE id = ?", (job_id,)
        )
        await self.conn.commit()

    async def pending_count(self) -> int:
        """Sessions staged but not yet transcribed."""
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM transcription_queue WHERE status != 'done'"
        )
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0
