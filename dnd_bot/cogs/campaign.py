"""/campaign commands.

Listing is open to anyone who can run the bot's commands. Creating a campaign,
or changing which voice channel it owns, is gated like the other commands that
decide what future recordings are called and filed under.
"""

import logging

import discord
from discord.ext import commands

from ..access import require_privileged
from ..campaigns import normalize_name
from ..db import CampaignConflict

log = logging.getLogger(__name__)


async def campaign_names(ctx: discord.AutocompleteContext) -> list[str]:
    rows = await ctx.bot.db.list_campaigns()
    typed = (ctx.value or "").casefold()
    return [row["name"] for row in rows if typed in row["name"].casefold()][:25]


async def find_campaign(db, name: str) -> dict | None:
    wanted = name.strip().casefold()
    for row in await db.list_campaigns():
        if row["name"].casefold() == wanted:
            return row
    return None


class CampaignCog(commands.Cog):
    campaign = discord.SlashCommandGroup("campaign", "Manage campaigns")

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        self.config = bot.config

    @campaign.command(name="list", description="Show all campaigns")
    async def list_campaigns(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        rows = await self.db.list_campaigns()
        if not rows:
            await ctx.respond("No campaigns yet. Create one with `/campaign create`.")
            return
        lines = ["Campaigns:"]
        for row in rows:
            channel = f" - <#{row['channel_id']}>" if row["channel_id"] else ""
            lines.append(f"- **{row['name']}**{channel} ({row['session_count']} sessions)")
        await ctx.respond("\n".join(lines)[:1900])

    @campaign.command(name="create", description="Create a campaign")
    async def create(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, "Campaign name"),
        voice_channel: discord.Option(
            discord.VoiceChannel, "Sessions recorded here belong to it", required=False
        ) = None,
    ) -> None:
        await ctx.defer()
        if not await require_privileged(ctx, self.config):
            return
        try:
            created = await self.db.create_campaign(
                name=normalize_name(name),
                channel_id=voice_channel.id if voice_channel else None,
            )
        except (ValueError, CampaignConflict) as exc:
            await ctx.respond(str(exc))
            return
        log.info("Campaign %s created", created["id"])
        where = f" Recording in {voice_channel.mention} will use it." if voice_channel else ""
        await ctx.respond(f"Created campaign **{created['name']}**.{where}")

    @campaign.command(name="channel", description="Set or clear a campaign's voice channel")
    async def set_channel(
        self,
        ctx: discord.ApplicationContext,
        campaign: discord.Option(str, "Campaign", autocomplete=campaign_names),
        voice_channel: discord.Option(
            discord.VoiceChannel, "Leave empty to clear", required=False
        ) = None,
    ) -> None:
        await ctx.defer()
        if not await require_privileged(ctx, self.config):
            return
        row = await find_campaign(self.db, campaign)
        if row is None:
            await ctx.respond("No such campaign.")
            return
        try:
            await self.db.update_campaign(
                row["id"], channel_id=voice_channel.id if voice_channel else None
            )
        except CampaignConflict as exc:
            await ctx.respond(str(exc))
            return
        await ctx.respond(
            f"**{row['name']}** now uses {voice_channel.mention}."
            if voice_channel
            else f"**{row['name']}** no longer has a default voice channel."
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(CampaignCog(bot))
