"""/init: report the total you rolled for initiative.

Players roll their own dice and pass the total to the DM's tracker in the
portal. When the portal is showing a battle, `/init` without a total rolls d20
plus the sheet's initiative bonus instead. Nothing is posted to the channel.
"""

import logging

import discord
from discord.ext import commands

from ..initiative import MODES, VALUE_MAX, VALUE_MIN, Cooldown, clean_name, roll_initiative
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

    @discord.slash_command(
        name="init",
        description="Tell the DM your initiative, or let the bot roll it from your sheet",
    )
    async def init(
        self,
        ctx: discord.ApplicationContext,
        total: discord.Option(
            int,
            "Your total, dice and modifiers included. Leave it out to roll with your sheet's bonus",
            min_value=VALUE_MIN,
            max_value=VALUE_MAX,
            required=False,
        ) = None,
        name: discord.Option(
            str, "For a character other than your usual one", required=False, max_length=40
        ) = None,
        mode: discord.Option(
            str,
            "Advantage or disadvantage, when the bot rolls",
            choices=list(MODES),
            required=False,
        ) = None,
    ) -> None:
        if not self._cooldown.ready(str(ctx.author.id)):
            await ctx.respond("Easy. Try again in a moment.", ephemeral=True)
            return
        if total is None:
            await self._roll(ctx, mode or "normal")
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

    async def _roll(self, ctx: discord.ApplicationContext, mode: str) -> None:
        """No total: roll with the bonus from the sheet in the battle the portal is showing."""
        entry = await self.db.roster_entry(ctx.author.id)
        if entry is None:
            await ctx.respond("No battle is waiting on you. Use `/init total:<n>`.", ephemeral=True)
            return
        value, breakdown = roll_initiative(int(entry["bonus"]), mode)
        await self.db.add_initiative(ctx.author.id, entry["label"], value, source="rolled")
        log.info("Initiative rolled for %s", entry["label"])
        label = discord.utils.escape_markdown(entry["label"])
        await ctx.respond(
            f"Rolled {breakdown} = **{value}** for **{label}**. The DM will see it.", ephemeral=True
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(InitiativeCog(bot))
