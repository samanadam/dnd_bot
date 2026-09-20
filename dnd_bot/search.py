"""Full-text search across delivered transcripts.

The index is SQLite FTS5 in the bot's own database and is built lazily: a search
brings every stale session up to date first. A session is stale when its
transcript file changed, or when what it should be shown as changed - the
campaign it is filed under, its speaker relabel map, or that campaign's word
corrections. Indexing the text as it will be *read* means a search for a
corrected name finds the passage, and filing a session under another campaign
moves its hits with it.

FTS5 is compiled into the SQLite that ships with Python almost everywhere, but
not everywhere. If it is missing, `available()` is False and the route says so;
nothing else in the bot depends on this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiosqlite

from . import paths
from .db import Database
from .transcripts import TranscriptMissing, TranscriptReader

log = logging.getLogger(__name__)

# Sessions indexed per search call. A first search over a long history finishes
# over a few calls instead of holding one request open for minutes.
INDEX_BATCH = 15
MAX_TOKENS = 8
MAX_LIMIT = 100
SNIPPET_TOKENS = 18

_TOKEN = re.compile(r"\w+", re.UNICODE)


class SearchUnavailable(RuntimeError):
    """This SQLite build has no FTS5."""


@dataclass
class Hit:
    session_id: str
    session_name: str | None
    started_at: str
    campaign_id: str | None
    campaign_name: str | None
    seq: int
    speaker: str
    clock: str | None
    start: float | None
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_name": self.session_name,
            "started_at": self.started_at,
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "seq": self.seq,
            "speaker": self.speaker,
            "clock": self.clock,
            "start": self.start,
            "snippet": self.snippet,
        }


def match_expression(query: str) -> str:
    """Turn what a person typed into an FTS5 expression that cannot be syntax.

    Only word characters survive, and each word is quoted, so FTS operators
    (`OR`, `NEAR`, `*`, `-`, quotes, column filters) typed into the box are
    just words. The last word is a prefix match, so results narrow as you type.
    """
    tokens = _TOKEN.findall(query)[:MAX_TOKENS]
    if not tokens:
        raise ValueError("Type at least one word to search for.")
    quoted = [f'"{token}"' for token in tokens]
    quoted[-1] += "*"
    return " ".join(quoted)


def index_key(mtime_ns: int, campaign_id: str | None, relabel_json: str, corrections: list) -> str:
    view = json.dumps([campaign_id, relabel_json, corrections], ensure_ascii=False, sort_keys=True)
    return f"{mtime_ns}:{hashlib.sha1(view.encode('utf-8')).hexdigest()[:16]}"  # noqa: S324


class TranscriptIndex:
    def __init__(self, db: Database, reader: TranscriptReader) -> None:
        self.db = db
        self.reader = reader
        self._ready: bool | None = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        return self.db.conn

    async def available(self) -> bool:
        if self._ready is not None:
            return self._ready
        try:
            await self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5("
                "text, speaker UNINDEXED, session_id UNINDEXED, seq UNINDEXED, "
                "start UNINDEXED, clock UNINDEXED, "
                "tokenize = 'unicode61 remove_diacritics 2')"
            )
            await self.conn.execute(
                "CREATE TABLE IF NOT EXISTS transcript_index_state ("
                " session_id TEXT PRIMARY KEY, key TEXT NOT NULL)"
            )
            await self.conn.commit()
            self._ready = True
        except aiosqlite.OperationalError:
            log.warning("SQLite has no FTS5; transcript search is disabled")
            self._ready = False
        return self._ready

    async def _stored_keys(self) -> dict[str, str]:
        cursor = await self.conn.execute("SELECT session_id, key FROM transcript_index_state")
        return {row["session_id"]: row["key"] for row in await cursor.fetchall()}

    async def refresh(self, campaign: str | None = None) -> int:
        """Bring stale sessions up to date; returns how many are still stale."""
        if not await self.available():
            raise SearchUnavailable
        async with self._lock:
            sessions = await self.db.list_sessions(limit=1000, campaign=campaign)
            stored = await self._stored_keys()
            corrections: dict[str, list] = {}
            stale: list[tuple[dict[str, Any], str]] = []
            for row in sessions:
                if not row["transcribed"]:
                    continue
                json_path = paths.transcript_json_path(self.reader.sessions_root, row["id"])
                md_path = paths.transcript_md_path(self.reader.sessions_root, row["id"])
                source = json_path if json_path.is_file() else md_path
                if not source.is_file():
                    continue
                campaign_id = row.get("campaign_id")
                if campaign_id and campaign_id not in corrections:
                    corrections[campaign_id] = await self.db.campaign_corrections(campaign_id)
                key = index_key(
                    source.stat().st_mtime_ns,
                    campaign_id,
                    row.get("relabel_json") or "{}",
                    corrections.get(campaign_id, []) if campaign_id else [],
                )
                if stored.get(row["id"]) != key:
                    stale.append((row, key))
            for row, key in stale[:INDEX_BATCH]:
                await self._index_one(row, key, corrections.get(row.get("campaign_id"), []))
            return max(0, len(stale) - INDEX_BATCH)

    async def _index_one(self, row: dict[str, Any], key: str, corrections: list) -> None:
        relabel = json.loads(row.get("relabel_json") or "{}")
        try:
            parsed = await asyncio.to_thread(
                self.reader.read, row["id"], relabel=relabel or None, corrections=corrections
            )
        except TranscriptMissing:
            return
        await self.conn.execute("DELETE FROM transcript_fts WHERE session_id = ?", (row["id"],))
        await self.conn.executemany(
            "INSERT INTO transcript_fts (text, speaker, session_id, seq, start, clock) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (s["text"], s["speaker"], row["id"], seq, s.get("start"), s.get("clock"))
                for seq, s in enumerate(parsed.segments)
            ],
        )
        await self.conn.execute(
            "INSERT INTO transcript_index_state (session_id, key) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET key = excluded.key",
            (row["id"], key),
        )
        await self.conn.commit()

    async def forget(self, session_id: str) -> None:
        if not await self.available():
            return
        await self.conn.execute("DELETE FROM transcript_fts WHERE session_id = ?", (session_id,))
        await self.conn.execute(
            "DELETE FROM transcript_index_state WHERE session_id = ?", (session_id,)
        )
        await self.conn.commit()

    async def search(
        self, query: str, *, campaign: str | None = None, limit: int = 30
    ) -> tuple[list[Hit], int]:
        expression = match_expression(query)
        limit = max(1, min(limit, MAX_LIMIT))
        pending = await self.refresh(campaign)
        clause, args = "", []
        if campaign == "unassigned":
            clause = " AND s.campaign_id IS NULL"
        elif campaign is not None:
            clause, args = " AND s.campaign_id = ?", [campaign]
        cursor = await self.conn.execute(
            "SELECT f.session_id, f.seq, f.speaker, f.start, f.clock, "
            f"snippet(transcript_fts, 0, '[[', ']]', '...', {SNIPPET_TOKENS}) AS snip, "
            "s.name AS session_name, s.start_time, s.campaign_id, c.name AS campaign_name "
            "FROM transcript_fts f JOIN sessions s ON s.id = f.session_id "
            "LEFT JOIN campaigns c ON c.id = s.campaign_id "
            f"WHERE transcript_fts MATCH ?{clause} AND s.deleted_at IS NULL ORDER BY rank LIMIT ?",
            (expression, *args, limit),
        )
        hits = [
            Hit(
                session_id=row["session_id"],
                session_name=row["session_name"],
                started_at=row["start_time"],
                campaign_id=row["campaign_id"],
                campaign_name=row["campaign_name"],
                seq=int(row["seq"]),
                speaker=row["speaker"],
                clock=row["clock"],
                start=row["start"],
                snippet=row["snip"],
            )
            for row in await cursor.fetchall()
        ]
        return hits, pending
