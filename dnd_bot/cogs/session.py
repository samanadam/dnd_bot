"""/session commands.

Recording a channel stays open: anyone currently in the voice channel can start,
stop or cancel that channel's session, because the people in the channel are the
people being recorded.

Reaching backwards is gated. `/session transcript`, `/session export` and
`/session recover` act on sessions the caller may have had nothing to do with,
and the first two publish that session's words - or its raw audio - into
whatever channel the caller happens to be in. See `access.py`.

All bot-facing text is English, independent of the transcript language.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .. import paths
from ..access import require_privileged
from ..exports import ExportError, build_export, fits_discord_upload, size_mb
from ..recorder import RecordingError
from ..timeutil import format_duration, from_iso, to_local, utcnow

log = logging.getLogger(__name__)


def _voice_channel_of(ctx: discord.ApplicationContext) -> discord.VoiceChannel | None:
    voice = getattr(ctx.author, "voice", None)
    return voice.channel if voice else None


class SessionCog(commands.Cog):
    """Recording lifecycle commands."""

    session = discord.SlashCommandGroup("session", "Record and manage D&D sessions")

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.manager = bot.manager

    # -- start / stop ------------------------------------------------------

    @session.command(name="start", description="Start recording your current voice channel")
    async def start(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, "Name for this session", required=False) = None,  # noqa: F821
    ) -> None:
        await ctx.defer()
        channel = _voice_channel_of(ctx)
        if channel is None:
            await ctx.respond("Join a voice channel first, then run `/session start`.")
            return
        try:
            session = await self.manager.start(
                channel=channel,
                text_channel_id=ctx.channel_id,
                invoker=ctx.author,
                name=name,
            )
        except RecordingError as exc:
            await ctx.respond(str(exc))
            return
        except discord.Forbidden:
            await ctx.respond(f"I lack permission to join **{channel.name}**.")
            return

        members = ", ".join(sorted(session.labels.values())) or "nobody yet"
        lines = [
            f"Recording **{session.name}** in **{channel.name}**.",
            f"Session id: `{session.session_id}`",
            f"In channel now: {members}",
            "Stop with `/session stop`.",
        ]
        lines += session.warnings
        await ctx.respond("\n".join(lines))

    @session.command(name="stop", description="Stop your channel's session and hand it over")
    async def stop(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        channel = _voice_channel_of(ctx)
        if channel is None:
            await ctx.respond("Join the voice channel whose session you want to stop.")
            return
        result = await self.manager.stop(ctx.guild_id, channel.id, reason="manual")
        if result is None:
            await ctx.respond(f"No session is being recorded in **{channel.name}**.")
            return

        handover = (
            "Audio is saved and waiting for the transcriber to collect it. "
            "The transcript will be posted here once it comes back."
            if result.enqueued
            else "Nothing was handed over for transcription."
        )
        lines = [
            f"Stopped **{result.name}** (`{result.session_id}`).",
            f"Duration: {format_duration(result.duration_seconds)} | "
            f"Speakers: {len(result.speakers)}",
            handover,
        ]
        if result.warnings:
            lines.append("Warnings: " + "; ".join(result.warnings))
        await ctx.respond("\n".join(lines))

    @session.command(name="cancel", description="Stop and discard your channel's session")
    async def cancel(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        channel = _voice_channel_of(ctx)
        if channel is None:
            await ctx.respond("Join the voice channel whose session you want to cancel.")
            return
        session_id = await self.manager.cancel(ctx.guild_id, channel.id)
        if session_id is None:
            await ctx.respond(f"No session is being recorded in **{channel.name}**.")
            return
        await ctx.respond(
            f"Cancelled session `{session_id}` in **{channel.name}**. "
            "All partial audio for it was deleted."
        )

    # -- inspection --------------------------------------------------------

    @session.command(name="status", description="Show active recording sessions")
    async def status(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        channel = _voice_channel_of(ctx)
        now = utcnow()

        if channel is not None:
            session = self.manager.get(ctx.guild_id, channel.id)
            if session is not None:
                speakers = ", ".join(sorted(session.labels.values())) or "none detected yet"
                await ctx.respond(
                    f"**{session.name}** in **{session.channel_name}**\n"
                    f"Id: `{session.session_id}`\n"
                    f"Running for {format_duration(session.elapsed_seconds(now))}\n"
                    f"Speakers heard: {speakers}"
                )
                return

        active = self.manager.sessions_in_guild(ctx.guild_id)
        if not active:
            waiting = await self.db.awaiting_transcription()
            if not waiting:
                await ctx.respond(
                    "No active recording sessions, and nothing awaiting a transcript."
                )
                return
            lines = [f"No active recording sessions. {len(waiting)} awaiting a transcript:"]
            lines += [
                f"- `{row['session_id']}` {row.get('name') or 'unnamed'} ({row['status']})"
                for row in waiting[:10]
            ]
            await ctx.respond("\n".join(lines)[:1900])
            return
        lines = ["Active sessions:"]
        lines += [
            f"- **{s.name}** in **{s.channel_name}** "
            f"({format_duration(s.elapsed_seconds(now))}) - `{s.session_id}`"
            for s in active
        ]
        await ctx.respond("\n".join(lines))

    @session.command(name="list", description="List past completed sessions")
    async def list_sessions(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        rows = await self.db.list_sessions(limit=20)
        if not rows:
            await ctx.respond("No completed sessions yet.")
            return
        lines = ["Recent sessions:"]
        for row in rows:
            start = from_iso(row["start_time"])
            end = from_iso(row.get("end_time"))
            duration = format_duration((end - start).total_seconds()) if start and end else "?"
            when = to_local(start, self.config.tz).strftime("%Y-%m-%d %H:%M") if start else "?"
            flag = "transcribed" if row.get("transcribed") else "pending transcription"
            lines.append(
                f"- `{row['id']}` **{row.get('name') or 'unnamed'}** "
                f"#{row.get('channel_name')} - {when} ({duration}, {flag})"
            )
        await ctx.respond("\n".join(lines)[:1900])

    @session.command(name="transcript", description="Re-post a past session's transcript")
    async def transcript(
        self,
        ctx: discord.ApplicationContext,
        session_id: discord.Option(str, "Session id from /session list"),  # noqa: F821
    ) -> None:
        await ctx.defer()
        if not await require_privileged(ctx, self.config):
            return
        row = await self.db.get_session(session_id)
        if row is None:
            await ctx.respond(f"No session found with id `{session_id}`.")
            return
        md_path = paths.transcript_md_path(self.config.sessions_dir, session_id)
        if not md_path.exists():
            state = (
                "still waiting on the transcriber"
                if not row.get("transcribed")
                else "missing on disk"
            )
            await ctx.respond(f"No transcript for `{session_id}` - it is {state}.")
            return
        try:
            await ctx.respond(
                f"Transcript for **{row.get('name') or session_id}**:",
                file=discord.File(str(md_path)),
            )
        except discord.Forbidden:
            await ctx.respond(
                "I cannot attach files in this channel. The transcript is on the server at "
                f"`{md_path}`."
            )

    # -- recovery / export -------------------------------------------------

    @session.command(name="recover", description="Finalize a session left open by a crash")
    async def recover(
        self,
        ctx: discord.ApplicationContext,
        session_id: discord.Option(str, "Session id reported at startup"),  # noqa: F821
    ) -> None:
        await ctx.defer()
        if not await require_privileged(ctx, self.config):
            return
        try:
            result = await self.manager.recover(session_id)
        except RecordingError as exc:
            await ctx.respond(str(exc))
            return
        lines = [
            f"Recovered **{result.name}** (`{result.session_id}`) with "
            f"{len(result.speakers)} speaker(s).",
            (
                "Queued for transcription."
                if result.enqueued
                else "It was already queued for transcription."
            ),
        ]
        if result.warnings:
            lines.append("Warnings: " + "; ".join(result.warnings))
        await ctx.respond("\n".join(lines))

    @session.command(name="export", description="Zip a session's audio and transcript")
    async def export(
        self,
        ctx: discord.ApplicationContext,
        session_id: discord.Option(str, "Session id from /session list"),  # noqa: F821
    ) -> None:
        await ctx.defer()
        if not await require_privileged(ctx, self.config):
            return
        if await self.db.get_session(session_id) is None:
            await ctx.respond(f"No session found with id `{session_id}`.")
            return
        try:
            # Zipping hours of audio would block the event loop, so it runs in a thread.
            archive = await asyncio.to_thread(
                build_export,
                self.config.sessions_dir,
                self.config.exports_dir,
                session_id,
                self.config.outbox_dir,
            )
        except ExportError as exc:
            await ctx.respond(str(exc))
            return

        limit = self.config.export_max_discord_upload_mb
        if not fits_discord_upload(archive, limit):
            await ctx.respond(
                f"Export is {size_mb(archive):.1f} MB, over the {limit} MB upload limit.\n"
                f"It is on the server at `{archive}`."
            )
            return
        try:
            await ctx.respond(
                f"Export for `{session_id}` ({size_mb(archive):.1f} MB):",
                file=discord.File(str(archive)),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.respond(
                f"Could not upload the export ({exc}). It is on the server at `{archive}`."
            )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(SessionCog(bot))
