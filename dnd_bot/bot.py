"""Bot wiring: intents, cogs, background tasks, startup checks, shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from datetime import datetime, timedelta

import discord

from .backup import backup_database, prune_backups
from .cleanup import run_cleanup
from .config import Config, ConfigError, load_config
from .db import Database
from .inbox import InboxDelivery, InboxFetcher
from .notify import DiscordNotifier
from .r2 import OUTBOX_PREFIX, R2Error, R2Store
from .recorder import SessionManager
from .recovery import scan_for_recoverable
from .timeutil import to_iso, utcnow
from .uploader import OutboxUploader

log = logging.getLogger("dnd_bot")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.voice_states = True
    intents.members = True
    intents.message_content = False
    return intents


class DnDBot(discord.Bot):
    def __init__(self, config: Config, db: Database) -> None:
        super().__init__(intents=build_intents(), debug_guilds=[config.guild_id])
        self.config = config
        self.db = db
        self.notifier = DiscordNotifier(self, config.admin_user_id)
        self.manager = SessionManager(self, db, config)
        self.deliveries = InboxDelivery(db, config, self.notifier)

        # With R2 the handover is object storage rather than a shared
        # filesystem; the local outbox becomes a staging area an uploader
        # drains. Both are None on the local backend, and every use is guarded.
        self.store = R2Store.from_config(config) if config.uses_r2 else None
        self.uploader = OutboxUploader(self.store, config) if self.store else None
        self.fetcher = InboxFetcher(self.store, config) if self.store else None

        # NOT `_tasks`: discord.Client uses that name for its own set of
        # internal tasks and calls .add() on it, so shadowing it here
        # crashes the library the moment it schedules anything.
        self._background_tasks: list[asyncio.Task] = []
        self._started = False
        self._shutting_down = False

        self.load_extension("dnd_bot.cogs.session")
        self.load_extension("dnd_bot.cogs.character")

    # -- lifecycle ---------------------------------------------------------

    async def on_ready(self) -> None:
        if self._started:
            log.info("Reconnected as %s", self.user)
            return
        self._started = True
        log.info("Logged in as %s (guild %s)", self.user, self.config.guild_id)

        # Heartbeat first: the healthcheck reads it, and the startup checks
        # below reach the network, so a slow one must not read as unhealthy.
        self._background_tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))

        await self._check_object_storage()
        await self._report_recoverable()

        self._background_tasks += [
            asyncio.create_task(self._inbox_loop(), name="inbox"),
            asyncio.create_task(self._cleanup_loop(), name="cleanup"),
            asyncio.create_task(self._backup_loop(), name="backup"),
        ]
        if self.uploader is not None:
            self._background_tasks.append(asyncio.create_task(self._upload_loop(), name="upload"))
        log.info(
            "Bot ready; handover via %s; %s session(s) waiting on the transcriber",
            "Cloudflare R2" if self.config.uses_r2 else "the local filesystem",
            await self.db.pending_count(),
        )

    async def _check_object_storage(self) -> None:
        """Prove R2 is reachable now, rather than at the end of a session.

        Bad credentials do not stop the bot recording - they stop it handing
        anything over, silently, while sessions pile up on disk. Non-fatal on
        purpose: recording locally and uploading later is better than refusing
        to start because Cloudflare is briefly unreachable.
        """
        if self.store is None:
            return
        try:
            await asyncio.to_thread(self.store.list_keys, f"{OUTBOX_PREFIX}/")
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            message = (
                f"Cannot reach the R2 bucket `{self.config.r2_bucket}`: "
                f"`{type(exc).__name__}`. Recording still works, but finished "
                "sessions will stay on this disk until it is fixed. Check the "
                "R2_* settings, then restart."
            )
            log.error("R2 is not reachable: %s", exc)
            await self.notifier.send_dm(self.config.admin_user_id, message)
            return
        log.info("R2 bucket %s is reachable", self.config.r2_bucket)

    async def _report_recoverable(self) -> None:
        """A crash-orphaned session is worthless unless somebody is told about it."""
        recoverable = await scan_for_recoverable(self.db, self.config.sessions_dir)
        if not recoverable:
            return
        lines = [
            f"{len(recoverable)} session(s) were left unfinished by a crash or hard restart. "
            "Their audio is still on disk - finish each one with `/session recover <id>`:"
        ]
        lines += [
            f"- `{row['id']}` {row.get('name') or 'unnamed'} (#{row.get('channel_name')})"
            for row in recoverable[:10]
        ]
        message = "\n".join(lines)
        for row in recoverable[:1]:
            await self.notifier.notify_session(row, message)
        await self.notifier.send_dm(self.config.admin_user_id, message)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        with contextlib.suppress(Exception):
            await self.manager.handle_voice_state_update(member, before, after)

    async def on_application_command_error(
        self, ctx: discord.ApplicationContext, error: Exception
    ) -> None:
        # The exception text routinely carries server filesystem paths and
        # occasionally configuration values, so it goes to the log - which the
        # operator can read - rather than into a Discord channel.
        # py-cord hands the error in as an argument rather than re-raising it,
        # so there is no active exception for log.exception to pick up - it used
        # to log "NoneType: None" and throw the real traceback away.
        log.error(
            "Command %s failed",
            getattr(ctx.command, "qualified_name", "?"),
            exc_info=error,
        )
        message = (
            f"Something went wrong (`{type(error).__name__}`). "
            "The details are in the bot's logs."
        )
        with contextlib.suppress(discord.HTTPException):
            if ctx.response.is_done():
                await ctx.followup.send(message)
            else:
                await ctx.respond(message)

    async def shutdown(self) -> None:
        """Flush live recordings to disk before the process exits."""
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("Shutting down: finalizing active sessions")
        with contextlib.suppress(Exception):
            results = await self.manager.shutdown_all()
            for result in results:
                log.info("Finalized %s on shutdown (queued=%s)", result.session_id, result.enqueued)
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.close()
        await self.db.close()
        log.info("Shutdown complete")

    # -- background loops --------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Container healthcheck reads the mtime of this file."""
        while True:
            try:
                self.config.heartbeat_path.write_text(to_iso(utcnow()), encoding="utf-8")
            except OSError:
                log.exception("Could not write heartbeat file")
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    async def _upload_loop(self) -> None:
        """Drain the local staging area into R2, freeing the disk it used."""
        while True:
            try:
                await self.uploader.run_once()
            except Exception:  # noqa: BLE001 - the loop must outlive any single failure
                log.exception("Upload pass failed")
            await asyncio.sleep(self.config.upload_interval_seconds)

    async def _inbox_loop(self) -> None:
        """Post transcripts as the transcriber returns them."""
        while True:
            try:
                if self.fetcher is not None:
                    # Downloads land in the local inbox, so delivery below is
                    # identical whichever backend brought the transcript here.
                    await self.fetcher.run_once()
                await self.deliveries.run_once()
            except Exception:  # noqa: BLE001 - the loop must outlive any single failure
                log.exception("Inbox delivery pass failed")
            await asyncio.sleep(self.config.inbox_poll_seconds)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await run_cleanup(self.db, self.config, self.notifier)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("Cleanup pass failed")
            await asyncio.sleep(self.config.cleanup_interval_seconds)

    async def _backup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_backup())
            try:
                await asyncio.to_thread(
                    backup_database, self.config.db_path, self.config.backups_dir
                )
                await asyncio.to_thread(
                    prune_backups, self.config.backups_dir, self.config.db_backup_keep_days
                )
            except Exception:  # noqa: BLE001 - a failed backup must not kill the bot
                log.exception("Database backup failed")

    def _seconds_until_backup(self, now: datetime | None = None) -> float:
        """Once a day at 05:00 local time - off-peak for a D&D group."""
        now = now or datetime.now(self.config.tz)
        target = now.replace(hour=5, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()


def install_signal_handlers(loop: asyncio.AbstractEventLoop, bot: DnDBot) -> None:
    def handler() -> None:
        log.info("Received termination signal")
        loop.create_task(bot.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, AttributeError):  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: handler())


async def run() -> int:
    configure_logging()
    try:
        config = load_config()
        config.ensure_dirs()
    except ConfigError as exc:
        # Startup misconfiguration is a human problem, not a bug: report it as a
        # readable message rather than a traceback in a crash-looping container.
        log.critical("Configuration error: %s", exc)
        return 2

    db = Database(config.db_path)
    await db.connect()

    try:
        bot = DnDBot(config, db)
    except R2Error as exc:
        # Same reasoning as ConfigError above: a misconfigured bucket is a human
        # problem, and a traceback in a restart loop is a miserable way to meet it.
        log.critical("Object storage error: %s", exc)
        await db.close()
        return 2
    install_signal_handlers(asyncio.get_running_loop(), bot)
    try:
        await bot.start(config.discord_token)
    except discord.LoginFailure:
        log.critical("Discord rejected the token. Check DISCORD_TOKEN.")
        return 2
    finally:
        await bot.shutdown()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
