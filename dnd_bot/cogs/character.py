"""/character commands.

Mappings are global (one character per Discord user) and anyone may edit anyone
else's - access control here is deliberately open, matching the rest of the bot.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


class CharacterCog(commands.Cog):
    character = discord.SlashCommandGroup("character", "Map Discord users to characters")

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    @character.command(name="set", description="Map a Discord user to a character name")
    async def set_character(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.Member, "The player"),  # noqa: F821
        character_name: discord.Option(str, "Character name to use in transcripts"),  # noqa: F821
    ) -> None:
        await ctx.defer()
        name = character_name.strip()
        if not name:
            await ctx.respond("Character name cannot be empty.")
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
        user: discord.Option(discord.Member, "The player"),  # noqa: F821
    ) -> None:
        await ctx.defer()
        await self.db.clear_character(user.id)
        await ctx.respond(
            f"Cleared the character mapping for {user.display_name}. "
            "Their nickname or username will be used instead."
        )

    @character.command(name="list", description="Show all character mappings")
    async def list_characters(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        mapping = await self.db.character_map()
        if not mapping:
            await ctx.respond("No character mappings set. Use `/character set`.")
            return
        lines = ["Character mappings:"]
        for user_id, name in sorted(mapping.items(), key=lambda item: item[1].lower()):
            member = ctx.guild.get_member(int(user_id)) if ctx.guild else None
            who = member.display_name if member else f"user {user_id}"
            lines.append(f"- **{name}** - {who}")
        await ctx.respond("\n".join(lines)[:1900])


def setup(bot: discord.Bot) -> None:
    bot.add_cog(CharacterCog(bot))
