"""Speaker label resolution.

Priority is fixed: character name (/character set) > server nickname > username.
Labels are resolved once per session and stored with the session so a transcript
stays self-contained even if the mapping changes later.
"""

from __future__ import annotations

from collections.abc import Mapping


def resolve_label(
    user_id: str,
    character_map: Mapping[str, str] | None = None,
    nickname: str | None = None,
    username: str | None = None,
) -> str:
    """Return the display label for a speaker, falling back down the priority chain."""
    character_map = character_map or {}
    character = character_map.get(str(user_id))
    for candidate in (character, nickname, username):
        if candidate and candidate.strip():
            return candidate.strip()
    return f"User {user_id}"


def resolve_participants(
    members: list[tuple[str, str | None, str | None]],
    character_map: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve a list of (user_id, nickname, username) tuples into {user_id: label}."""
    return {
        str(user_id): resolve_label(str(user_id), character_map, nickname, username)
        for user_id, nickname, username in members
    }
