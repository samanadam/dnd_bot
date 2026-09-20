"""SQLite access layer.

All structured state lives here rather than in flat files so concurrent sessions
in different voice channels cannot race on shared writes. Audio blobs stay on
the filesystem; this database only holds metadata.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .campaigns import relabel_map
from .initiative import MAX_AGE_HOURS as INITIATIVE_MAX_AGE_HOURS
from .initiative import MAX_PENDING as MAX_INITIATIVE
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


class CampaignConflict(ValueError):
    """A campaign name or voice channel is already taken."""


_CAMPAIGN_FIELDS = frozenset({"name", "channel_id", "language", "archived"})
_CAMPAIGN_TAKEN = "A campaign with that name, or one already using that voice channel, exists."


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
        # Which model produced the transcript is not knowable here any more:
        # this half does not transcribe. The transcriber records it, and the
        # column stays for the sessions written before the split.
        model_used: str | None = None,
        campaign_id: str | None = None,
        base_labels: dict[str, str] | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO sessions (
                id, name, guild_id, channel_id, channel_name, text_channel_id,
                started_by_user_id, start_time, participants_json, language, model_used,
                campaign_id, base_labels_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                campaign_id,
                json.dumps(base_labels or {}, ensure_ascii=False),
            ),
        )
        await self.conn.commit()

    _SESSION_SELECT = (
        "SELECT s.*, c.name AS campaign_name FROM sessions s "
        "LEFT JOIN campaigns c ON c.id = s.campaign_id"
    )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cursor = await self.conn.execute(f"{self._SESSION_SELECT} WHERE s.id = ?", (session_id,))
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

    async def merge_participants(
        self,
        session_id: str,
        participants: dict[str, str],
        base_labels: dict[str, str] | None = None,
    ) -> None:
        """Add newly-seen speakers without dropping the ones already recorded."""
        row = await self.get_session(session_id)
        if row is None:
            return
        current = json.loads(row["participants_json"] or "{}")
        current.update(participants)
        fields: dict[str, Any] = {"participants_json": json.dumps(current, ensure_ascii=False)}
        if base_labels:
            base = json.loads(row["base_labels_json"] or "{}")
            base.update(base_labels)
            fields["base_labels_json"] = json.dumps(base, ensure_ascii=False)
        await self.update_session(session_id, **fields)

    async def set_offsets(self, session_id: str, offsets: dict[str, float]) -> None:
        await self.update_session(session_id, offsets_json=json.dumps(offsets, ensure_ascii=False))

    async def list_sessions(
        self, limit: int = 25, campaign: str | None = None
    ) -> list[dict[str, Any]]:
        """Finished sessions, newest first.

        `campaign` is None for all of them, "unassigned" for those with no
        campaign, or a campaign id.
        """
        clause, args = "", []
        if campaign == "unassigned":
            clause = " AND s.campaign_id IS NULL"
        elif campaign is not None:
            clause, args = " AND s.campaign_id = ?", [campaign]
        cursor = await self.conn.execute(
            f"{self._SESSION_SELECT} WHERE s.completed = 1 AND s.cancelled = 0 "
            f"AND s.deleted_at IS NULL{clause} "
            "ORDER BY s.start_time DESC LIMIT ?",
            (*args, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def list_trashed_sessions(self) -> list[dict[str, Any]]:
        """Sessions waiting in the trash, most recently deleted first."""
        cursor = await self.conn.execute(
            f"{self._SESSION_SELECT} WHERE s.deleted_at IS NOT NULL ORDER BY s.deleted_at DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def list_open_sessions(self) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE completed = 0 AND cancelled = 0 "
            "AND deleted_at IS NULL ORDER BY start_time"
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

    async def character_map(self, campaign_id: str | None = None) -> dict[str, str]:
        """The global character map, with a campaign's own names laid over it."""
        cursor = await self.conn.execute("SELECT user_id, character_name FROM characters")
        mapping = {row["user_id"]: row["character_name"] for row in await cursor.fetchall()}
        if campaign_id:
            mapping.update(await self.campaign_characters(campaign_id))
        return mapping

    # -- campaigns ---------------------------------------------------------

    async def create_campaign(
        self, *, name: str, channel_id: int | None = None, language: str | None = None
    ) -> dict[str, Any]:
        campaign_id = secrets.token_hex(6)
        try:
            await self.conn.execute(
                "INSERT INTO campaigns (id, name, channel_id, language, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    campaign_id,
                    name,
                    str(channel_id) if channel_id else None,
                    language,
                    to_iso(utcnow()),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise CampaignConflict(_CAMPAIGN_TAKEN) from exc
        await self.conn.commit()
        created = await self.get_campaign(campaign_id)
        assert created is not None
        return created

    async def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        cursor = await self.conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_campaigns(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE c.archived = 0 "
        cursor = await self.conn.execute(
            "SELECT c.*, COUNT(s.id) AS session_count FROM campaigns c "
            "LEFT JOIN sessions s ON s.campaign_id = c.id AND s.completed = 1 AND s.cancelled = 0 "
            "AND s.deleted_at IS NULL "
            f"{where}GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def update_campaign(self, campaign_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - _CAMPAIGN_FIELDS
        if unknown:
            raise ValueError(f"Cannot update campaign field(s): {', '.join(sorted(unknown))}")
        if fields:
            if fields.get("channel_id") is not None:
                fields["channel_id"] = str(fields["channel_id"])
            assignments = ", ".join(f"{key} = ?" for key in fields)
            try:
                await self.conn.execute(
                    f"UPDATE campaigns SET {assignments} WHERE id = ?",
                    (*fields.values(), campaign_id),
                )
            except sqlite3.IntegrityError as exc:
                raise CampaignConflict(_CAMPAIGN_TAKEN) from exc
            await self.conn.commit()
        return await self.get_campaign(campaign_id)

    async def campaign_for_channel(self, channel_id: int) -> dict[str, Any] | None:
        cursor = await self.conn.execute(
            "SELECT * FROM campaigns WHERE channel_id = ? AND archived = 0", (str(channel_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def replace_terms(self, campaign_id: str, terms: list[str]) -> None:
        await self.conn.execute("DELETE FROM campaign_terms WHERE campaign_id = ?", (campaign_id,))
        await self.conn.executemany(
            "INSERT INTO campaign_terms (campaign_id, term, position) VALUES (?, ?, ?)",
            [(campaign_id, term, position) for position, term in enumerate(terms)],
        )
        await self.conn.commit()

    async def campaign_terms(self, campaign_id: str) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT term FROM campaign_terms WHERE campaign_id = ? ORDER BY position",
            (campaign_id,),
        )
        return [row["term"] for row in await cursor.fetchall()]

    async def replace_corrections(self, campaign_id: str, pairs: list[tuple[str, str]]) -> None:
        await self.conn.execute(
            "DELETE FROM campaign_corrections WHERE campaign_id = ?", (campaign_id,)
        )
        await self.conn.executemany(
            "INSERT INTO campaign_corrections (campaign_id, heard, correct) VALUES (?, ?, ?)",
            [(campaign_id, heard, correct) for heard, correct in pairs],
        )
        await self.conn.commit()

    async def campaign_corrections(self, campaign_id: str) -> list[tuple[str, str]]:
        cursor = await self.conn.execute(
            "SELECT heard, correct FROM campaign_corrections WHERE campaign_id = ? ORDER BY rowid",
            (campaign_id,),
        )
        return [(row["heard"], row["correct"]) for row in await cursor.fetchall()]

    async def set_campaign_character(
        self, campaign_id: str, user_id: int, character_name: str
    ) -> None:
        await self.conn.execute(
            "INSERT INTO campaign_characters (campaign_id, user_id, character_name, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(campaign_id, user_id) DO UPDATE SET "
            "character_name = excluded.character_name, updated_at = excluded.updated_at",
            (campaign_id, str(user_id), character_name, to_iso(utcnow())),
        )
        await self.conn.commit()

    async def assign_session_campaign(
        self, session_id: str, campaign_id: str | None
    ) -> dict[str, Any] | None:
        """Tag a finished session, or clear the tag.

        Nothing on disk changes. The relabel map recorded here is what a
        transcript is shown with; clearing the tag rebuilds it from the plain
        (non-character) labels plus the global character map, so the old
        campaign's character names disappear with the tag.
        """
        row = await self.get_session(session_id)
        if row is None:
            return None
        if campaign_id is not None and await self.get_campaign(campaign_id) is None:
            raise LookupError("No such campaign.")
        participants = json.loads(row["participants_json"] or "{}")
        base = json.loads(row["base_labels_json"] or "{}")
        relabel = relabel_map(participants, base, await self.character_map(campaign_id))
        await self.update_session(
            session_id,
            campaign_id=campaign_id,
            relabel_json=json.dumps(relabel, ensure_ascii=False),
        )
        return await self.get_session(session_id)

    async def clear_campaign_character(self, campaign_id: str, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM campaign_characters WHERE campaign_id = ? AND user_id = ?",
            (campaign_id, str(user_id)),
        )
        await self.conn.commit()

    async def campaign_characters(self, campaign_id: str) -> dict[str, str]:
        cursor = await self.conn.execute(
            "SELECT user_id, character_name FROM campaign_characters WHERE campaign_id = ?",
            (campaign_id,),
        )
        return {row["user_id"]: row["character_name"] for row in await cursor.fetchall()}

    # -- initiative --------------------------------------------------------

    async def add_initiative(self, user_id: int, label: str, value: int) -> None:
        """Record a player's total, replacing their earlier one under that name."""
        await self.conn.execute(
            "INSERT INTO initiative_reports (user_id, label, value, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(user_id, label) DO UPDATE SET "
            "value = excluded.value, created_at = excluded.created_at",
            (str(user_id), label, value, to_iso(utcnow())),
        )
        # Bounded: keep only the newest few, so a misbehaving client cannot grow it.
        await self.conn.execute(
            "DELETE FROM initiative_reports WHERE id NOT IN ("
            " SELECT id FROM initiative_reports ORDER BY created_at DESC, id DESC LIMIT ?)",
            (MAX_INITIATIVE,),
        )
        await self.conn.commit()

    async def list_initiative(self) -> list[dict[str, Any]]:
        """Pending totals, oldest first. Expired ones are dropped on the way."""
        cutoff = to_iso(utcnow() - timedelta(hours=INITIATIVE_MAX_AGE_HOURS))
        await self.conn.execute("DELETE FROM initiative_reports WHERE created_at < ?", (cutoff,))
        await self.conn.commit()
        cursor = await self.conn.execute(
            "SELECT id, label, value, created_at FROM initiative_reports ORDER BY created_at, id"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def clear_initiative(self, report_id: int | None = None) -> int:
        """Drop one pending total, or all of them. Returns how many went."""
        if report_id is None:
            cursor = await self.conn.execute("DELETE FROM initiative_reports")
        else:
            cursor = await self.conn.execute(
                "DELETE FROM initiative_reports WHERE id = ?", (report_id,)
            )
        await self.conn.commit()
        return cursor.rowcount

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
            "WHERE q.status != 'done' AND s.deleted_at IS NULL ORDER BY q.queued_at"
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
            "SELECT COUNT(*) AS n FROM transcription_queue q "
            "LEFT JOIN sessions s ON s.id = q.session_id "
            "WHERE q.status != 'done' AND s.deleted_at IS NULL"
        )
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0
