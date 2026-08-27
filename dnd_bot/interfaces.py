"""Protocols shared by background tasks.

Kept in its own module so the queue worker and cleanup task can be imported -
and tested - without pulling in discord.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class Notifier(Protocol):
    """What background tasks need in order to talk to Discord."""

    async def send_channel(
        self, channel_id: int | None, content: str, file_path: Path | None = None
    ) -> bool: ...

    async def send_dm(self, user_id: int | None, content: str) -> bool: ...

    async def notify_session(
        self, session: dict[str, Any], content: str, file_path: Path | None = None
    ) -> None: ...

    async def notify_failure(self, session: dict[str, Any], content: str) -> None: ...
