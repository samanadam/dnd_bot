"""Per-channel recording session management.

All state is keyed by (guild_id, channel_id) - there is deliberately no single
global "current session", because a side conversation in another voice channel
must be able to record independently of the main table.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import discord

from . import paths
from .config import Config
from .db import Database
from .finalize import finalize_session_audio
from .labels import resolve_label
from .outbox import publish
from .sinks import DiskSink
from .timeutil import to_iso, utcnow

log = logging.getLogger(__name__)

SessionKey = tuple[int, int]


class RecordingError(RuntimeError):
    """User-facing failure while starting or stopping a recording."""


@dataclass
class ActiveSession:
    session_id: str
    name: str
    guild_id: int
    channel_id: int
    channel_name: str
    text_channel_id: int | None
    started_by_user_id: int
    start_time: datetime
    voice_client: discord.VoiceClient
    sink: DiskSink
    labels: dict[str, str] = field(default_factory=dict)
    offsets: dict[str, float] = field(default_factory=dict)
    alone_task: asyncio.Task | None = None
    monitor_task: asyncio.Task | None = None
    stopping: bool = False

    @property
    def key(self) -> SessionKey:
        return (self.guild_id, self.channel_id)

    def elapsed_seconds(self, now: datetime | None = None) -> float:
        return max(0.0, ((now or utcnow()) - self.start_time).total_seconds())

    def to_row(self) -> dict[str, Any]:
        """Shape used by the notifier, which only needs routing fields."""
        return {
            "id": self.session_id,
            "name": self.name,
            "text_channel_id": str(self.text_channel_id) if self.text_channel_id else None,
            "started_by_user_id": str(self.started_by_user_id),
        }


@dataclass
class StopResult:
    session_id: str
    name: str
    duration_seconds: float
    speakers: list[str]
    warnings: list[str]
    enqueued: bool


class SessionManager:
    """Owns the live recordings and the transitions in and out of them."""

    def __init__(self, bot: discord.Bot, db: Database, config: Config) -> None:
        self.bot = bot
        self.db = db
        self.config = config
        self.active: dict[SessionKey, ActiveSession] = {}
        self._locks: dict[SessionKey, asyncio.Lock] = {}

    # -- helpers -----------------------------------------------------------

    def get(self, guild_id: int, channel_id: int) -> ActiveSession | None:
        return self.active.get((guild_id, channel_id))

    def lock_for(self, key: SessionKey) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def sessions_in_guild(self, guild_id: int) -> list[ActiveSession]:
        return [s for s in self.active.values() if s.guild_id == guild_id]

    async def _resolve_members(self, channel: discord.VoiceChannel) -> dict[str, str]:
        character_map = await self.db.character_map()
        return {
            str(member.id): resolve_label(str(member.id), character_map, member.nick, member.name)
            for member in channel.members
            if not member.bot
        }

    # -- start -------------------------------------------------------------

    async def start(
        self,
        *,
        channel: discord.VoiceChannel,
        text_channel_id: int | None,
        invoker: discord.Member,
        name: str | None,
    ) -> ActiveSession:
        key = (channel.guild.id, channel.id)
        async with self.lock_for(key):
            if key in self.active:
                raise RecordingError(
                    f"A session is already being recorded in **{channel.name}**. "
                    "Stop it first with `/session stop`."
                )

            # Discord allows one voice connection per account per guild, so a
            # single bot token cannot record two channels at the same time.
            # State is still keyed per channel; this is a platform ceiling, not
            # an internal one. Running a second bot token would lift it.
            busy = self.sessions_in_guild(channel.guild.id)
            if busy:
                other = busy[0]
                raise RecordingError(
                    f"I am already recording **{other.name}** in **{other.channel_name}**. "
                    "Discord only lets one bot account sit in one voice channel per server, "
                    "so that session has to be stopped before I can record "
                    f"**{channel.name}**."
                )

            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.connect:
                raise RecordingError(f"I do not have permission to connect to **{channel.name}**.")
            if not permissions.view_channel:
                raise RecordingError(f"I cannot see **{channel.name}**.")
            if channel.user_limit and len(channel.members) >= channel.user_limit:
                if not permissions.move_members:
                    raise RecordingError(f"**{channel.name}** is full, so I cannot join.")

            session_id = str(uuid.uuid4())
            start_time = utcnow()
            display_name = name or f"{channel.name} {start_time.strftime('%Y-%m-%d %H:%M')}"

            try:
                voice_client = await channel.connect(timeout=30.0, reconnect=True)
            except TimeoutError as exc:
                raise RecordingError(
                    f"Timed out connecting to **{channel.name}**. Try again in a moment."
                ) from exc
            except discord.ClientException as exc:
                raise RecordingError(f"Could not join **{channel.name}**: {exc}") from exc

            paths.ensure_session_dirs(self.config.sessions_dir, session_id)
            labels = await self._resolve_members(channel)

            await self.db.create_session(
                session_id=session_id,
                name=display_name,
                guild_id=channel.guild.id,
                channel_id=channel.id,
                channel_name=channel.name,
                text_channel_id=text_channel_id,
                started_by_user_id=invoker.id,
                start_time=to_iso(start_time),
                participants=labels,
                language=self.config.transcribe_language,
                model_used=self.config.whisper_model,
            )

            session = ActiveSession(
                session_id=session_id,
                name=display_name,
                guild_id=channel.guild.id,
                channel_id=channel.id,
                channel_name=channel.name,
                text_channel_id=text_channel_id,
                started_by_user_id=invoker.id,
                start_time=start_time,
                voice_client=voice_client,
                sink=self._build_sink(session_id, 0.0, {}),
                labels=labels,
            )
            self.active[key] = session
            self._start_recording(session)
            session.monitor_task = asyncio.create_task(self._monitor_connection(session))
            log.info("Session %s started in #%s by %s", session_id, channel.name, invoker.id)
            return session

    def _build_sink(
        self, session_id: str, base_offset: float, known_offsets: dict[str, float]
    ) -> DiskSink:
        return DiskSink(
            paths.raw_dir(self.config.sessions_dir, session_id),
            flush_interval=self.config.flush_interval_seconds,
            on_new_speaker=self._make_speaker_callback(session_id),
            base_offset=base_offset,
            known_offsets=known_offsets,
        )

    def _make_speaker_callback(self, session_id: str):
        """The sink runs on the voice thread; hop back onto the event loop."""
        loop = asyncio.get_running_loop()

        def callback(user_id: str, offset: float) -> None:
            asyncio.run_coroutine_threadsafe(
                self._register_speaker(session_id, user_id, offset), loop
            )

        return callback

    async def _register_speaker(self, session_id: str, user_id: str, offset: float) -> None:
        """Persist a newly-heard speaker's label and offset as soon as they talk."""
        session = next((s for s in self.active.values() if s.session_id == session_id), None)
        try:
            if session is not None and user_id not in session.offsets:
                session.offsets[user_id] = offset
                if user_id not in session.labels:
                    character_map = await self.db.character_map()
                    guild = self.bot.get_guild(session.guild_id)
                    member = guild.get_member(int(user_id)) if guild else None
                    session.labels[user_id] = resolve_label(
                        user_id,
                        character_map,
                        getattr(member, "nick", None),
                        getattr(member, "name", None),
                    )
                    await self.db.merge_participants(session_id, {user_id: session.labels[user_id]})
                await self.db.set_offsets(session_id, session.offsets)
        except Exception:  # noqa: BLE001 - never let a callback kill the session
            log.exception("Failed registering speaker %s for %s", user_id, session_id)

    def _start_recording(self, session: ActiveSession) -> None:
        def finished(sink: DiskSink, *args) -> None:  # noqa: ANN001 - py-cord callback
            log.debug("Recording callback fired for %s", session.session_id)

        session.voice_client.start_recording(session.sink, finished)

    # -- connection resilience --------------------------------------------

    async def _monitor_connection(self, session: ActiveSession) -> None:
        """Reconnect a dropped voice link; finalize gracefully if we cannot."""
        attempts = 0
        try:
            while not session.stopping:
                await asyncio.sleep(5)
                if session.stopping:
                    return
                if session.voice_client.is_connected():
                    attempts = 0
                    continue

                attempts += 1
                log.warning(
                    "Voice connection lost for session %s (attempt %s/%s)",
                    session.session_id,
                    attempts,
                    self.config.voice_reconnect_attempts,
                )
                if attempts > self.config.voice_reconnect_attempts:
                    await self._finalize_after_connection_loss(session)
                    return
                try:
                    await self._resume_recording(session)
                    log.info("Voice connection restored for session %s", session.session_id)
                    attempts = 0
                except Exception:  # noqa: BLE001 - retried on the next tick
                    log.exception("Reconnect attempt failed for %s", session.session_id)
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise

    async def _resume_recording(self, session: ActiveSession) -> None:
        channel = self.bot.get_channel(session.channel_id)
        if channel is None:
            raise RecordingError("Voice channel no longer exists")
        with contextlib.suppress(Exception):
            session.sink.cleanup()
        # The dead voice client is still registered for this guild, and connect()
        # refuses a second one, so tear it down first.
        with contextlib.suppress(Exception):
            await session.voice_client.disconnect(force=True)
        voice_client = await channel.connect(timeout=30.0, reconnect=True)
        session.voice_client = voice_client
        session.sink = self._build_sink(
            session.session_id, session.elapsed_seconds(), dict(session.offsets)
        )
        self._start_recording(session)

    async def _finalize_after_connection_loss(self, session: ActiveSession) -> None:
        log.error(
            "Giving up on voice reconnection for %s; finalizing what we captured",
            session.session_id,
        )
        result = await self.stop(session.guild_id, session.channel_id, reason="connection_lost")
        channel = self.bot.get_channel(session.text_channel_id or 0)
        if channel is not None and result is not None:
            with contextlib.suppress(discord.HTTPException, discord.Forbidden):
                await channel.send(
                    f"Lost the voice connection to **{session.channel_name}** and could not "
                    f"reconnect. Session **{result.name}** was finalized with "
                    f"{len(result.speakers)} speaker(s) and queued for transcription."
                )

    # -- auto-stop when alone ---------------------------------------------

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot and member.id == self.bot.user.id:
            return
        for channel_id in {
            getattr(before.channel, "id", None),
            getattr(after.channel, "id", None),
        }:
            if channel_id is None:
                continue
            session = self.get(member.guild.id, channel_id)
            if session is None:
                continue
            self._evaluate_alone(session)

    def _evaluate_alone(self, session: ActiveSession) -> None:
        channel = self.bot.get_channel(session.channel_id)
        humans = [m for m in getattr(channel, "members", []) if not m.bot]
        if humans:
            if session.alone_task is not None:
                session.alone_task.cancel()
                session.alone_task = None
            return
        if session.alone_task is None or session.alone_task.done():
            session.alone_task = asyncio.create_task(self._auto_stop_later(session))

    async def _auto_stop_later(self, session: ActiveSession) -> None:
        """Grace period first - a brief reconnect should not end the session."""
        try:
            await asyncio.sleep(self.config.alone_grace_seconds)
            channel = self.bot.get_channel(session.channel_id)
            humans = [m for m in getattr(channel, "members", []) if not m.bot]
            if humans or session.stopping:
                return
            log.info("Auto-stopping session %s: channel is empty", session.session_id)
            result = await self.stop(session.guild_id, session.channel_id, reason="alone")
            text_channel = self.bot.get_channel(session.text_channel_id or 0)
            if text_channel is not None and result is not None:
                with contextlib.suppress(discord.HTTPException, discord.Forbidden):
                    await text_channel.send(
                        f"Everyone left **{session.channel_name}**, so session "
                        f"**{result.name}** was stopped automatically and queued for "
                        "transcription."
                    )
        except asyncio.CancelledError:
            return

    # -- stop / cancel -----------------------------------------------------

    async def stop(
        self, guild_id: int, channel_id: int, reason: str = "manual"
    ) -> StopResult | None:
        key = (guild_id, channel_id)
        async with self.lock_for(key):
            session = self.active.get(key)
            if session is None:
                return None
            session.stopping = True
            self.active.pop(key, None)

            for task in (session.alone_task, session.monitor_task):
                if task is not None and task is not asyncio.current_task():
                    task.cancel()

            await self._teardown_voice(session)

            written, warnings = finalize_session_audio(
                self.config.sessions_dir, session.session_id, self.config.audio_format
            )
            end_time = utcnow()
            await self.db.set_offsets(session.session_id, session.offsets)
            await self.db.update_session(
                session.session_id,
                end_time=to_iso(end_time),
                completed=1,
            )
            enqueued = False
            if written:
                row = await self.db.get_session(session.session_id)
                staging_warnings = await self._stage_for_transcription(row)
                warnings += staging_warnings
                if not staging_warnings:
                    await self.db.mark_exported(session.session_id)
                    enqueued = True
            else:
                warnings.append("No audio was captured, so there is nothing to transcribe.")

            log.info(
                "Session %s stopped (reason=%s, speakers=%s, queued=%s)",
                session.session_id,
                reason,
                len(written),
                enqueued,
            )
            return StopResult(
                session_id=session.session_id,
                name=session.name,
                duration_seconds=session.elapsed_seconds(end_time),
                speakers=sorted(session.labels.get(p.stem, p.stem) for p in written),
                warnings=warnings,
                enqueued=enqueued,
            )

    async def cancel(self, guild_id: int, channel_id: int) -> str | None:
        """Stop and discard: audio is deleted, nothing is queued."""
        key = (guild_id, channel_id)
        async with self.lock_for(key):
            session = self.active.get(key)
            if session is None:
                return None
            session.stopping = True
            self.active.pop(key, None)
            for task in (session.alone_task, session.monitor_task):
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
            await self._teardown_voice(session)

            import shutil

            session_dir = paths.session_dir(self.config.sessions_dir, session.session_id)
            shutil.rmtree(session_dir, ignore_errors=True)
            await self.db.update_session(
                session.session_id,
                cancelled=1,
                completed=1,
                end_time=to_iso(utcnow()),
            )
            log.info("Session %s cancelled and discarded", session.session_id)
            return session.session_id

    async def _stage_for_transcription(self, row: dict[str, Any] | None) -> list[str]:
        """Publish the finished session for the transcriber to collect.

        The audio is moved, not copied: this host does not transcribe, and on a
        small VPS a second copy is disk it does not have.
        """
        if row is None or not self.config.outbox_enabled:
            return []
        try:
            await asyncio.to_thread(
                publish,
                row,
                sessions_root=self.config.sessions_dir,
                outbox_root=self.config.outbox_dir,
                audio_format=self.config.audio_format,
                timezone_name=self.config.timezone_name,
                prompt_extra=self.config.whisper_prompt_extra,
                move=True,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal to the stop path
            log.exception("Could not stage session %s for the transcriber", row.get("id"))
            return [f"Could not stage this session for the transcriber: {exc}"]
        return []

    async def _teardown_voice(self, session: ActiveSession) -> None:
        with contextlib.suppress(Exception):
            if session.voice_client.recording:
                session.voice_client.stop_recording()
        with contextlib.suppress(Exception):
            session.sink.cleanup()
        with contextlib.suppress(Exception):
            await session.voice_client.disconnect(force=True)

    async def shutdown_all(self) -> list[StopResult]:
        """SIGTERM path: flush every live session to disk before the process dies."""
        results: list[StopResult] = []
        for key in list(self.active):
            try:
                result = await self.stop(*key, reason="shutdown")
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.exception("Failed stopping session %s during shutdown", key)
                continue
            if result is not None:
                results.append(result)
        return results

    # -- recovery ----------------------------------------------------------

    async def recover(self, session_id: str) -> StopResult:
        """Finalize and enqueue a session left open by a crash."""
        row = await self.db.get_session(session_id)
        if row is None:
            raise RecordingError(f"No session found with id `{session_id}`.")
        if any(s.session_id == session_id for s in self.active.values()):
            raise RecordingError("That session is still recording - use `/session stop`.")

        written, warnings = finalize_session_audio(
            self.config.sessions_dir, session_id, self.config.audio_format
        )
        if not written:
            raise RecordingError(f"No recoverable audio was found for session `{session_id}`.")

        end_time = row.get("end_time") or to_iso(utcnow())
        await self.db.update_session(session_id, completed=1, transcribed=0, end_time=end_time)
        row = await self.db.get_session(session_id) or row
        staging_warnings = await self._stage_for_transcription(row)
        warnings += staging_warnings
        enqueued = not staging_warnings
        if enqueued:
            await self.db.mark_exported(session_id)
        labels: dict[str, str] = json.loads(row.get("participants_json") or "{}")
        duration = 0.0
        start = row.get("start_time")
        if start:
            from .timeutil import from_iso

            start_dt, end_dt = from_iso(start), from_iso(end_time)
            if start_dt and end_dt:
                duration = max(0.0, (end_dt - start_dt).total_seconds())
        return StopResult(
            session_id=session_id,
            name=row.get("name") or session_id[:8],
            duration_seconds=duration,
            speakers=sorted(labels.get(p.stem, p.stem) for p in written),
            warnings=warnings,
            enqueued=enqueued,
        )
