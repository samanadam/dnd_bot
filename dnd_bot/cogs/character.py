"""/character commands.

Mappings are global unless a campaign is named: without one, a Discord user has a
single character everywhere; with `campaign:` they can be a different character
in that campaign, which then wins over the global name. Setting your own is open
to everyone, because a player naming their own character is the ordinary case.
Setting or clearing *somebody else's* is gated - the label chosen here is what
that person is called in every future transcript, so it is not a thing to leave
open to anyone who can type.
"""

import logging

import discord
from discord.ext import commands

from ..access import require_privileged
from .campaign import campaign_names, find_campaign

log = logging.getLogger(__name__)


class CharacterCog(commands.Cog):
    character = discord.SlashCommandGroup("character", "Map Discord users to characters")

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        self.config = bot.config

    async def _may_edit(self, ctx: discord.ApplicationContext, user: discord.Member) -> bool:
        """Anyone may name their own character; naming someone else's is gated."""
        if getattr(ctx.author, "id", None) == user.id:
            return True
        return await require_privileged(ctx, self.config)

    @character.command(name="set", description="Map a Discord user to a character name")
    async def set_character(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.Member, "The player"),
        character_name: discord.Option(str, "Character name to use in transcripts"),
        campaign: discord.Option(
            str, "Only for this campaign", required=False, autocomplete=campaign_names
        ) = None,
    ) -> None:
        await ctx.defer()
        if not await self._may_edit(ctx, user):
            return
        name = character_name.strip()
        if not name:
            await ctx.respond("Character name cannot be empty.")
            return
        if campaign:
            row = await find_campaign(self.db, campaign)
            if row is None:
                await ctx.respond("No such campaign. See `/campaign list`.")
                return
            await self.db.set_campaign_character(row["id"], user.id, name)
            log.info("Character mapping set in campaign %s: %s -> %s", row["id"], user.id, name)
            await ctx.respond(
                f"{user.display_name} will appear as **{name}** in future "
                f"**{row['name']}** transcripts. Already-written transcripts are unchanged."
            )
            return
        await self.db.set_character(user.id, name)
        log.info("Character mapping set: %s -> %s", user.id, name)
        await ctx.respond(
            f"{user.display_name} will appear as **{name}** in future transcripts. "
            "Already-written transcripts are unchanged."
        )

    @character.command(name="clear", description="Remove a user's character mapping")
    async def clear_character(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.Member, "The player"),
        campaign: discord.Option(
            str, "Only this campaign's mapping", required=False, autocomplete=campaign_names
        ) = None,
    ) -> None:
        await ctx.defer()
        if not await self._may_edit(ctx, user):
            return
        if campaign:
            row = await find_campaign(self.db, campaign)
            if row is None:
                await ctx.respond("No such campaign. See `/campaign list`.")
                return
            await self.db.clear_campaign_character(row["id"], user.id)
            await ctx.respond(
                f"Cleared {user.display_name}'s mapping for **{row['name']}**. "
                "The global name, or their nickname, will be used there."
            )
            return
        await self.db.clear_character(user.id)
        await ctx.respond(
            f"Cleared the character mapping for {user.display_name}. "
            "Their nickname or username will be used instead."
        )

    @character.command(name="list", description="Show all character mappings")
    async def list_characters(
        self,
        ctx: discord.ApplicationContext,
        campaign: discord.Option(
            str, "Show this campaign's mappings", required=False, autocomplete=campaign_names
        ) = None,
    ) -> None:
        await ctx.defer()
        title = "Character mappings:"
        if campaign:
            row = await find_campaign(self.db, campaign)
            if row is None:
                await ctx.respond("No such campaign. See `/campaign list`.")
                return
            mapping = await self.db.campaign_characters(row["id"])
            title = f"Character mappings for **{row['name']}**:"
        else:
            mapping = await self.db.character_map()
        if not mapping:
            await ctx.respond("No character mappings set. Use `/character set`.")
            return
        lines = [title]
        for user_id, name in sorted(mapping.items(), key=lambda item: item[1].lower()):
            member = ctx.guild.get_member(int(user_id)) if ctx.guild else None
            who = member.display_name if member else f"user {user_id}"
            lines.append(f"- **{name}** - {who}")
        await ctx.respond("\n".join(lines)[:1900])


def setup(bot: discord.Bot) -> None:
    bot.add_cog(CharacterCog(bot))
