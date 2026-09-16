"""Music playback, sharing one voice connection with the recorder.

Discord gives a bot a single voice connection per guild, and `SessionManager`
refuses to make a second one. So the rule here is ownership: when a recording
is live, music *borrows* that connection and may only ever stop playback on it;
when there is no recording, music connects for itself and may hang up. Getting
this wrong does not produce a muted track, it ends somebody's session - which
is why every transition goes through one place.

The other trap is `SessionManager._resume_recording`: after a voice drop it
force-disconnects and replaces `session.voice_client`, which silently kills
playback. `rebind()` is the hook that puts the music back.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import discord

from .net import ffmpeg_protocol_args, looks_like_a_flag

log = logging.getLogger(__name__)

LOOP_MODES = ("off", "track", "queue")

# Streams drop; tell ffmpeg to reconnect rather than ending the track.
STREAM_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
# -vn because a YouTube "audio" stream can still carry a video track.
FFMPEG_OPTIONS = "-vn -ar 48000 -ac 2"


class MusicError(RuntimeError):
    """User-facing playback failure. Becomes a 409."""


@lru_cache(maxsize=8)
def _play_accepts_signal_type(play_function) -> bool:
    try:
        parameters = inspect.signature(play_function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins have no signature
        return False
    if "signal_type" in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _accepts_signal_type(voice_client) -> bool:
    return _play_accepts_signal_type(type(voice_client).play)


@dataclass
class Track:
    id: str
    title: str
    source: str
    uri: str
    duration_seconds: float | None = None
    # Monotonic deadline after which `uri` must be resolved again. Streams are
    # signed and short-lived; a cached file never expires, so this stays None.
    expires_at: float | None = None

    def is_stale(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:  # noqa: D401
        # No requester id: this object ends up in an API response, and the
        # public-facing rule is that Discord user ids never leave the process.
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class GuildPlayer:
    """Everything one guild's playback knows about itself."""

    guild_id: int
    channel_id: int | None = None
    # "recording" = borrowed from a live session, "music" = ours to hang up.
    owner: str | None = None
    voice_client: Any | None = None
    queue: deque[Track] = field(default_factory=deque)
    current: Track | None = None
    loop_mode: str = "off"
    volume: float = 0.3
    source: Any | None = None
    started_at: float = 0.0
    paused_total: float = 0.0
    paused_at: float | None = None
    # Bumped on every start and every halt. py-cord calls the `after` callback
    # from a finally block, so a track that was interrupted reports itself
    # finished exactly like one that ran out; the callback carries the epoch it
    # was started with, and a stale one is ignored. Without this, every skip
    # advances twice and every "play now" is overtaken by what it interrupted.
    epoch: int = 0

    def position_seconds(self, now: float) -> float:
        """Elapsed play time, counted off the clock rather than off frames.

        Frame counting drifts and would have to be threaded back from the
        player thread; the only consumer is the resume offset, which does not
        need that precision.
        """
        if not self.current:
            return 0.0
        paused = self.paused_total + (now - self.paused_at if self.paused_at else 0.0)
        return max(0.0, now - self.started_at - paused)


class MusicManager:
    """Owns every guild's player and the truce with the recorder."""

    def __init__(
        self,
        bot,
        config,
        sources: dict[str, Any] | None = None,
        source_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bot = bot
        self.config = config
        self.sources = sources or {}
        # Injected so tests never need ffmpeg on PATH.
        self._source_factory = source_factory or self._ffmpeg_source
        self._clock = clock
        self._players: dict[int, GuildPlayer] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    # -- helpers -----------------------------------------------------------

    def player(self, guild_id: int) -> GuildPlayer:
        player = self._players.get(guild_id)
        if player is None:
            player = GuildPlayer(guild_id=guild_id, volume=self.config.music_default_volume)
            self._players[guild_id] = player
        return player

    def lock_for(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def _ffmpeg_source(uri: str, volume: float, before_options: str = "") -> Any:
        """PCM, not Opus: PCMVolumeTransformer cannot touch an Opus source, and
        live volume is worth more here than the saved re-encode."""
        audio = discord.FFmpegPCMAudio(
            uri, before_options=before_options or None, options=FFMPEG_OPTIONS
        )
        return discord.PCMVolumeTransformer(audio, volume=volume)

    def _recording_client(self, guild_id: int):
        """The live recording's voice client in this guild, if there is one."""
        manager = getattr(self.bot, "manager", None)
        if manager is None:
            return None
        for session in manager.sessions_in_guild(guild_id):
            client = getattr(session, "voice_client", None)
            if client is not None and client.is_connected():
                return client
        return None

    async def _acquire_voice(self, guild_id: int, channel_id: int | None):
        """Borrow the recording's connection, or make one of our own."""
        player = self.player(guild_id)

        borrowed = self._recording_client(guild_id)
        if borrowed is not None:
            player.voice_client = borrowed
            player.owner = "recording"
            player.channel_id = getattr(getattr(borrowed, "channel", None), "id", channel_id)
            return borrowed

        if player.voice_client is not None and player.voice_client.is_connected():
            return player.voice_client

        if channel_id is None:
            raise MusicError("Not connected to a voice channel; name one to join.")
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if channel is None:
            raise MusicError("No such voice channel.")
        try:
            client = await channel.connect(timeout=30.0, reconnect=True)
        except Exception as exc:  # noqa: BLE001 - surfaced as a 409
            raise MusicError(f"Could not join the voice channel: {type(exc).__name__}") from exc
        player.voice_client = client
        player.owner = "music"
        player.channel_id = channel_id
        return client

    async def _fresh(self, track: Track) -> Track:
        """Re-resolve a track whose stream URL has expired.

        A link queued half an hour ago is very likely dead by the time it
        reaches the front: YouTube signs stream URLs with a short deadline. The
        alternative is ffmpeg failing on a 403 and the queue silently skipping.
        """
        if not track.is_stale(self._clock()):
            return track
        source = self.sources.get(track.source)
        if source is None:
            raise MusicError("That track's source is no longer available.")
        log.info("Re-resolving %s; its stream URL expired", track.title)
        try:
            return await source.resolve(track.id)
        except Exception as exc:  # noqa: BLE001 - surfaced as a playback failure
            raise MusicError(f"Could not refresh that track: {type(exc).__name__}") from exc

    def _start(self, player: GuildPlayer, track: Track, seek: float = 0.0) -> None:
        """Hand one track to the voice client. Raises MusicError, never leaks.

        The ffmpeg protocol whitelist is the important line here: without it a
        URI is free to name file:// or concat: and read this host's disk.
        """
        local = track.source == "r2"
        if not track.uri or looks_like_a_flag(track.uri):
            raise MusicError("That track has no usable source.")

        before = ffmpeg_protocol_args(local=local)
        if not local:
            before = f"{before} {STREAM_BEFORE_OPTIONS}"
        if seek > 0:
            before = f"{before} -ss {seek:.2f}".strip()

        source = self._source_factory(track.uri, volume=player.volume, before_options=before)
        player.epoch += 1
        callback = self._after_callback(player.guild_id, player.epoch)

        # signal_type tunes the Opus encoder for music and exists only on newer
        # py-cord. Asking the signature beats catching TypeError: that would
        # also swallow an unrelated TypeError from inside play() and then call
        # it a second time, starting two players on one connection.
        kwargs = {"signal_type": "music"} if _accepts_signal_type(player.voice_client) else {}
        try:
            player.voice_client.play(source, after=callback, **kwargs)
        except discord.ClientException as exc:
            raise MusicError(str(exc)) from exc

        player.source = source
        player.current = track
        player.started_at = self._clock() - seek
        player.paused_total = 0.0
        player.paused_at = None

    def _after_callback(self, guild_id: int, epoch: int):
        """py-cord runs `after` on the player thread; hop back to the loop.

        Same shape as the sink's speaker callback in recorder.py, and for the
        same reason: touching player state or the voice client from that thread
        is how you get a corrupted queue and no traceback.
        """
        loop = asyncio.get_running_loop()

        def callback(error: Exception | None = None) -> None:
            # At shutdown the loop can be gone before ffmpeg's player thread
            # notices. Scheduling onto a closed loop raises there, on a thread
            # with nothing to catch it, and leaves a coroutine never awaited.
            if loop.is_closed():
                return
            with _suppress():
                asyncio.run_coroutine_threadsafe(self.on_track_end(guild_id, error, epoch), loop)

        return callback

    # -- playback ----------------------------------------------------------

    async def play(
        self,
        guild_id: int,
        track: Track,
        *,
        channel_id: int | None = None,
        position: str = "end",
    ) -> GuildPlayer:
        async with self.lock_for(guild_id):
            player = self.player(guild_id)
            if position not in {"now", "next", "end"}:
                raise MusicError("position must be now, next or end.")
            held = len(player.queue) + (1 if player.current else 0)
            if held >= self.config.music_max_queue:
                raise MusicError(f"The queue is full ({self.config.music_max_queue} tracks).")

            await self._acquire_voice(guild_id, channel_id)

            if player.current is None or position == "now":
                if player.current is not None:
                    self._halt(player)
                self._start(player, await self._fresh(track))
            elif position == "next":
                player.queue.appendleft(track)
            else:
                player.queue.append(track)
            return player

    def _halt(self, player: GuildPlayer) -> None:
        """Stop the current track without touching the connection or the queue.

        Bumping the epoch is what makes this safe: stopping fires the after
        callback regardless, and the callback it fires is now stale.
        """
        player.epoch += 1
        client = player.voice_client
        if client is not None:
            with _suppress():
                client.stop()
        if player.source is not None:
            with _suppress():
                player.source.cleanup()
        player.source = None
        player.current = None
        player.paused_at = None

    async def on_track_end(
        self, guild_id: int, error: Exception | None, epoch: int | None = None
    ) -> None:
        """Advance the queue. Called from the voice thread via the loop.

        `epoch` identifies the track whose end is being reported. A mismatch
        means this is the echo of a track we deliberately interrupted, and
        acting on it would skip whatever is playing now.
        """
        player = self.player(guild_id)
        if epoch is not None and epoch != player.epoch:
            log.debug("Ignoring a stale track-end callback in guild %s", guild_id)
            return
        if error is not None:
            log.error("Playback failed in guild %s", guild_id, exc_info=error)
        finished = player.current
        player.current = None
        player.source = None

        if finished is not None and player.loop_mode == "track":
            nxt = finished
        elif finished is not None and player.loop_mode == "queue":
            player.queue.append(finished)
            nxt = player.queue.popleft() if player.queue else None
        else:
            nxt = player.queue.popleft() if player.queue else None

        if nxt is None:
            return
        try:
            # A queued stream may have expired while it waited its turn; this
            # is the moment that matters, not the moment it was queued.
            self._start(player, await self._fresh(nxt))
        except MusicError:
            log.exception("Could not start the next track in guild %s", guild_id)

    async def skip(self, guild_id: int) -> GuildPlayer:
        player = self.player(guild_id)
        if player.current is None:
            raise MusicError("Nothing is playing.")
        self._halt(player)
        await self.on_track_end(guild_id, None)
        return player

    async def pause(self, guild_id: int) -> GuildPlayer:
        player = self.player(guild_id)
        if player.current is None or player.voice_client is None:
            raise MusicError("Nothing is playing.")
        player.voice_client.pause()
        player.paused_at = self._clock()
        return player

    async def resume(self, guild_id: int) -> GuildPlayer:
        player = self.player(guild_id)
        if player.current is None or player.voice_client is None:
            raise MusicError("Nothing is paused.")
        player.voice_client.resume()
        if player.paused_at is not None:
            player.paused_total += self._clock() - player.paused_at
            player.paused_at = None
        return player

    async def stop(self, guild_id: int) -> GuildPlayer:
        """Stop and clear. The connection is left exactly as it was."""
        player = self.player(guild_id)
        self._halt(player)
        player.queue.clear()
        return player

    async def set_volume(self, guild_id: int, volume: float) -> GuildPlayer:
        if not 0.0 <= volume <= 2.0:
            raise MusicError("volume must be between 0 and 2.")
        player = self.player(guild_id)
        player.volume = volume
        if player.source is not None:
            # A live attribute write on the transformer; no restart, no gap.
            player.source.volume = volume
        return player

    def set_loop(self, guild_id: int, mode: str) -> GuildPlayer:
        if mode not in LOOP_MODES:
            raise MusicError(f"loop must be one of {', '.join(LOOP_MODES)}.")
        player = self.player(guild_id)
        player.loop_mode = mode
        return player

    # -- queue -------------------------------------------------------------

    def clear_queue(self, guild_id: int) -> GuildPlayer:
        self.player(guild_id).queue.clear()
        return self.player(guild_id)

    def remove(self, guild_id: int, index: int) -> Track:
        player = self.player(guild_id)
        if not 0 <= index < len(player.queue):
            raise MusicError("No track at that position.")
        track = player.queue[index]
        del player.queue[index]
        return track

    def move(self, guild_id: int, source_index: int, target_index: int) -> GuildPlayer:
        player = self.player(guild_id)
        size = len(player.queue)
        if not (0 <= source_index < size and 0 <= target_index < size):
            raise MusicError("No track at that position.")
        items = list(player.queue)
        items.insert(target_index, items.pop(source_index))
        player.queue = deque(items)
        return player

    # -- the truce with the recorder ---------------------------------------

    async def join(self, guild_id: int, channel_id: int):
        """Connect without playing anything, so the portal can pre-join."""
        async with self.lock_for(guild_id):
            await self._acquire_voice(guild_id, channel_id)
            return self.player(guild_id)

    async def detach(self, guild_id: int, reason: str = "") -> None:
        """Release the voice connection before a recording claims it.

        A connection we borrowed is only stopped; hanging it up would end the
        recording that owns it. One we made ourselves is closed, so that
        SessionManager's own connect is not fighting a live client.
        """
        player = self._players.get(guild_id)
        if player is None:
            return
        was_playing = player.current
        self._halt(player)
        if was_playing is not None:
            # Preserve what was playing at the head of the queue so a rebind,
            # or the operator, can pick it straight back up.
            player.queue.appendleft(was_playing)

        if player.owner == "music" and player.voice_client is not None:
            with _suppress():
                await player.voice_client.disconnect(force=True)
        player.voice_client = None
        player.owner = None
        log.info("Music released the voice connection in guild %s (%s)", guild_id, reason)

    async def rebind(self, guild_id: int, voice_client, channel_id: int | None = None) -> None:
        """Re-attach to a connection the recorder just (re)created.

        Called after a session starts and after a reconnect replaces the voice
        client. Never raises: it runs inside the recorder's own paths, where an
        exception would cost a session rather than a song.
        """
        player = self._players.get(guild_id)
        if player is None:
            return
        position = player.position_seconds(self._clock())
        current = player.current

        player.voice_client = voice_client
        player.owner = "recording"
        if channel_id is not None:
            player.channel_id = channel_id
        player.source = None
        player.current = None

        if current is None and player.queue and self.config.music_resume_after_reconnect:
            current, position = player.queue.popleft(), 0.0
        if current is None or not self.config.music_resume_after_reconnect:
            return

        try:
            # Streams seek by range request, cached files by ffmpeg -ss. If
            # either refuses, the track restarts rather than stopping dead.
            self._start(player, current, seek=position)
            log.info("Resumed %s at +%.0fs after a reconnect", current.title, position)
        except Exception:  # noqa: BLE001 - a lost song must never cost a session
            log.exception("Could not resume playback in guild %s", guild_id)
            player.current = None
            player.source = None

    # -- state -------------------------------------------------------------

    def state_summary(self, guild_id: int | None = None) -> dict[str, Any]:
        guild_id = guild_id if guild_id is not None else self.config.guild_id
        player = self.player(guild_id)
        client = player.voice_client
        connected = bool(client is not None and client.is_connected())
        return {
            "connected": connected,
            "channel_id": str(player.channel_id) if player.channel_id else None,
            "owner": player.owner,
            "playing": bool(connected and client.is_playing()),
            "paused": bool(connected and client.is_paused()),
            "volume": player.volume,
            "loop": player.loop_mode,
            "position_seconds": round(player.position_seconds(self._clock()), 1),
            "current": player.current.to_dict() if player.current else None,
            "queue": [track.to_dict() for track in player.queue],
            "sources": {name: source.enabled for name, source in self.sources.items()},
        }


class _suppress:
    """contextlib.suppress(Exception), named so the intent reads at the call site."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            log.debug("Ignored %s during voice teardown", exc_type.__name__)
        return True
