"""Outbound messaging with explicit fallbacks.

Every send path here is wrapped: a missing permission, a deleted channel or a
closed DM must never take down a background task or lose the information.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import discord

from .interfaces import Notifier

log = logging.getLogger(__name__)

__all__ = ["DiscordNotifier", "Notifier"]


class DiscordNotifier:
    """Notifier backed by a live bot connection."""

    def __init__(self, bot: discord.Bot, admin_user_id: int | None = None) -> None:
        self.bot = bot
        self.admin_user_id = admin_user_id

    async def _resolve_channel(self, channel_id: int | None):
        if not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Cannot resolve text channel %s", channel_id)
                return None
        return channel

    async def send_channel(
        self, channel_id: int | None, content: str, file_path: Path | None = None
    ) -> bool:
        channel = await self._resolve_channel(channel_id)
        if channel is None:
            return False
        if file_path is not None and file_path.exists():
            try:
                await channel.send(content, file=discord.File(str(file_path)))
                return True
            except discord.Forbidden:
                # Most likely "Attach Files" is missing; fall back to a path.
                log.warning("Attach denied in channel %s, sending path instead", channel_id)
                content = f"{content}\n(Could not attach the file. It is on the server at "
                content += f"`{file_path}`.)"
            except discord.HTTPException as exc:
                log.warning("Attachment upload failed in %s: %s", channel_id, exc)
                content = f"{content}\n(Upload failed: {exc}. File is at `{file_path}`.)"
        try:
            await channel.send(content)
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Cannot send to channel %s: %s", channel_id, exc)
            return False

    async def send_dm(self, user_id: int | None, content: str) -> bool:
        if not user_id:
            return False
        try:
            user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
            await user.send(content)
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Cannot DM user %s: %s", user_id, exc)
            return False

    async def notify_session(
        self, session: dict[str, Any], content: str, file_path: Path | None = None
    ) -> None:
        channel_id = session.get("text_channel_id")
        ok = await self.send_channel(int(channel_id) if channel_id else None, content, file_path)
        if not ok:
            starter = session.get("started_by_user_id")
            await self.send_dm(int(starter) if starter else self.admin_user_id, content)

    async def notify_failure(self, session: dict[str, Any], content: str) -> None:
        """Failures go to both the originating channel and the session starter."""
        channel_id = session.get("text_channel_id")
        await self.send_channel(int(channel_id) if channel_id else None, content)
        starter = session.get("started_by_user_id")
        target = int(starter) if starter else self.admin_user_id
        await self.send_dm(target, content)
