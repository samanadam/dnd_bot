"""Party notes, Discord side: what is said in a campaign's notes channel.

The portal owns which channel belongs to which campaign. This cog pulls that
list every minute and reports messages written, edited or deleted in those
channels back to the portal, which turns them into party notes. Off unless
PORTAL_URL is set. Failures are logged and dropped: a missed report must never
hurt the bot, and the portal's own notes are the source of truth.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

REFRESH_SECONDS = 60
TIMEOUT = aiohttp.ClientTimeout(total=5)
MAX_TEXT = 1500


class PartyNotesCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.watched: set[int] = set()
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.portal_url and self.config.api_token)

    def cog_unload(self) -> None:
        self.refresh.cancel()
        if self._session is not None:
            asyncio.ensure_future(self._session.close())

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.enabled and not self.refresh.is_running():
            self.refresh.start()

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {self.config.api_token}"},
            )
        return self._session

    @tasks.loop(seconds=REFRESH_SECONDS)
    async def refresh(self) -> None:
        try:
            async with self._http().get(f"{self.config.portal_url}/api/bot-sync/party-notes") as r:
                if r.status != 200:
                    log.warning("Party note channel list refused (status %s)", r.status)
                    return
                rows = await r.json()
            self.watched = {int(row["channel_id"]) for row in rows}
        except (TimeoutError, aiohttp.ClientError, ValueError, KeyError, TypeError) as exc:
            log.warning("Could not fetch the party note channels: %s", exc)

    async def _report(self, event: str, channel_id: int, message_id: int, **extra) -> None:
        body = {"event": event, "channel_id": str(channel_id), "message_id": str(message_id)}
        body.update({k: v for k, v in extra.items() if v is not None})
        try:
            async with self._http().post(
                f"{self.config.portal_url}/api/bot-sync/party-notes", json=body
            ) as r:
                if r.status != 200:
                    log.warning("Party note %s refused by the portal (status %s)", event, r.status)
        except (TimeoutError, aiohttp.ClientError) as exc:
            log.warning("Could not report a party note %s: %s", event, exc)

    def _watching(self, channel_id: int | None) -> bool:
        return self.enabled and channel_id in self.watched

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self._watching(message.channel.id):
            return
        text = message.content.strip()[:MAX_TEXT]
        if not text:
            return
        await self._report(
            "create",
            message.channel.id,
            message.id,
            author_id=str(message.author.id),
            author_name=message.author.display_name,
            text=text,
        )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        data = payload.data
        author = data.get("author") or {}
        content = data.get("content")
        if author.get("bot") or content is None or not self._watching(payload.channel_id):
            return
        await self._report(
            "edit",
            payload.channel_id,
            payload.message_id,
            author_id=author.get("id"),
            text=content.strip()[:MAX_TEXT],
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if self._watching(payload.channel_id):
            await self._report("delete", payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if self._watching(payload.channel_id):
            for message_id in payload.message_ids:
                await self._report("delete", payload.channel_id, message_id)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(PartyNotesCog(bot))
