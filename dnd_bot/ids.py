"""Session identifiers.

A session id is three things at once: a database key, a local directory name,
and an object-key segment in R2. Bare UUIDs made the last of those unreadable -
`outbox/` listed in arbitrary order and no key said when anything was recorded,
so finding last Friday's game meant opening metadata.json after metadata.json.

Ids therefore lead with the local date and time and keep a random tail:

    2026-09-09-2130-a1b2c3d4

Lexicographic order is now chronological order, which is what makes a bucket
listing scannable. The time is local (the configured timezone, not UTC) because
the point is for a human to recognise their own session at a glance.

Nothing anywhere parses an id - `session_prefix` only checks it is a single
path segment - so ids minted before this change stay valid forever. `short_id`
is the one function that looks inside one, and it handles both shapes.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .timeutil import to_local

ID_TIME_FORMAT = "%Y-%m-%d-%H%M"
RANDOM_TAIL_LENGTH = 8

# 2026-09-09-2130-a1b2c3d4
DATED_ID = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-\d{{4}}-[0-9a-f]{{{RANDOM_TAIL_LENGTH}}}$")


def new_session_id(started_at: datetime, tz: ZoneInfo) -> str:
    """A sortable, human-readable id for a session starting at `started_at`."""
    local = to_local(started_at, tz)
    tail = uuid.uuid4().hex[:RANDOM_TAIL_LENGTH]
    return f"{local.strftime(ID_TIME_FORMAT)}-{tail}"


def is_dated(session_id: str) -> bool:
    return bool(DATED_ID.match(session_id))


def short_id(session_id: str) -> str:
    """The shortest fragment that still tells two sessions apart in a message.

    For a dated id that is the random tail - the date is already in the
    session's name and start time, so repeating it adds nothing. For an old
    UUID it is the leading eight characters, exactly as before.
    """
    if is_dated(session_id):
        return session_id.rsplit("-", 1)[-1]
    return session_id[:RANDOM_TAIL_LENGTH]
