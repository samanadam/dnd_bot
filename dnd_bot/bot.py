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
from .notify import DiscordNotifier
from .queue_worker import TranscriptionWorker
from .recorder import SessionManager
from .recovery import scan_for_recoverable
from .timeutil import to_iso, utcnow
from .transcription import WhisperTranscriber

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
        self.transcriber = WhisperTranscriber(
            config.whisper_model,
            device=config.whisper_device,
            compute_type=config.whisper_compute_type,
            download_root=os.environ.get("WHISPER_CACHE_DIR") or None,
            beam_size=config.whisper_beam_size,
            condition_on_previous_text=config.whisper_condition_on_previous_text,
            vad_min_silence_ms=config.whisper_vad_min_silence_ms,
            filter_hallucinations_enabled=config.filter_hallucinations,
        )
        self.notifier = DiscordNotifier(self, config.admin_user_id)
        self.manager = SessionManager(self, db, config)
        self.worker = TranscriptionWorker(db, config, self.transcriber, self.notifier)
        self._tasks: list[asyncio.Task] = []
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

        # Start the heartbeat before loading the model: the first run downloads
        # ~1.5 GB, which can easily outlast the healthcheck's start period, and
        # a container killed as "unhealthy" mid-download would never finish.
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))

        requeued = await self.db.reset_stuck_jobs()
        if requeued:
            log.warning("Re-queued %s job(s) left processing by a previous run", requeued)
        await self._report_recoverable()

        if not await self._preload_model():
            await self.close()
            return

        self._tasks += [
            asyncio.create_task(self.worker.run_forever(), name="transcription-worker"),
            asyncio.create_task(self._cleanup_loop(), name="cleanup"),
            asyncio.create_task(self._backup_loop(), name="backup"),
        ]
        log.info("Bot ready; %s pending transcription job(s)", await self.db.pending_count())

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

    async def _preload_model(self) -> bool:
        """Load Whisper once up front - never look healthy with broken transcription."""
        try:
            await asyncio.to_thread(self.transcriber.load)
            return True
        except Exception as exc:  # noqa: BLE001 - fatal, but must be reported first
            log.critical(
                "FATAL: could not load Whisper model %s: %s", self.config.whisper_model, exc
            )
            await self.notifier.send_dm(
                self.config.admin_user_id,
                f"The D&D recorder bot could not load the Whisper model "
                f"`{self.config.whisper_model}`: `{exc}`. Transcription is unavailable, "
                "so the bot is shutting down instead of running half-broken.",
            )
            return False

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
        log.exception("Command %s failed", getattr(ctx.command, "qualified_name", "?"))
        message = f"Something went wrong: `{type(error).__name__}: {error}`"
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
        self.worker.stop()
        with contextlib.suppress(Exception):
            results = await self.manager.shutdown_all()
            for result in results:
                log.info("Finalized %s on shutdown (queued=%s)", result.session_id, result.enqueued)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
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

    bot = DnDBot(config, db)
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
