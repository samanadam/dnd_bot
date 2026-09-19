"""/init: report the total you rolled for initiative.

Players roll their own dice. This only passes the number to the DM's tracker in
the portal; nothing here rolls, and nothing is posted to the channel.
"""

import logging

import discord
from discord.ext import commands

from ..initiative import VALUE_MAX, VALUE_MIN, Cooldown, clean_name
from ..labels import resolve_label

log = logging.getLogger(__name__)


class InitiativeCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        self._cooldown = Cooldown()

    async def _campaign_id(self, guild_id: int | None) -> str | None:
        """The campaign of whatever is being recorded, if anything is."""
        manager = getattr(self.bot, "manager", None)
        if manager is None or guild_id is None:
            return None
        for session in manager.sessions_in_guild(guild_id):
            if session.campaign_id:
                return session.campaign_id
        return None

    @discord.slash_command(name="init", description="Tell the DM the initiative you rolled")
    async def init(
        self,
        ctx: discord.ApplicationContext,
        total: discord.Option(
            int, "Your total, dice and modifiers included", min_value=VALUE_MIN, max_value=VALUE_MAX
        ),
        name: discord.Option(
            str, "For a character other than your usual one", required=False, max_length=40
        ) = None,
    ) -> None:
        if not self._cooldown.ready(str(ctx.author.id)):
            await ctx.respond("Easy. Try again in a moment.", ephemeral=True)
            return
        label = clean_name(name or "")
        if not label:
            characters = await self.db.character_map(await self._campaign_id(ctx.guild_id))
            label = clean_name(
                resolve_label(
                    str(ctx.author.id),
                    characters,
                    getattr(ctx.author, "nick", None),
                    getattr(ctx.author, "name", None),
                )
            )
        await self.db.add_initiative(ctx.author.id, label, int(total))
        log.info("Initiative reported for %s", label)
        await ctx.respond(f"Noted: **{label}** {total}. The DM will see it.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(InitiativeCog(bot))
