"""Environment-driven configuration. Nothing here is hardcoded at a call site."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value or ""


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int

    # Transcription happens elsewhere. These two travel with the audio as
    # metadata for whoever does it.
    transcribe_language: str = "tr"
    whisper_prompt_extra: str = ""

    data_dir: Path = Path("/data")
    audio_format: str = "opus"
    audio_retention_days: int = 7
    # A deleted session stays in the trash this long before it is removed for good.
    trash_retention_days: int = 7
    disk_warning_threshold_mb: int = 2000
    # How long a session is assumed to run, for the free-space check at
    # /session start. Raw capture is ~0.7 GB per speaker-hour.
    expected_session_hours: float = 4.0
    admin_user_id: int | None = None
    # Role allowed to run the commands that reach past the current session
    # (/session export, transcript, recover). Manage Guild works regardless.
    session_admin_role_id: int | None = None
    export_max_discord_upload_mb: int = 25

    timezone_name: str = "Europe/Istanbul"

    db_backup_keep_days: int = 14

    # Handover to an external transcriber.
    outbox_enabled: bool = True

    # Where the handover happens: a shared filesystem ("local", the transcriber
    # pulls over SSH) or Cloudflare R2 ("r2", both halves talk outbound only).
    storage_backend: str = "local"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    upload_interval_seconds: int = 120

    # HTTP API for the portal. Off unless asked for: enabling it puts control of
    # recordings behind one shared token, so it must be a deliberate choice.
    api_enabled: bool = False
    # Loopback by default. The container overrides this to 0.0.0.0 and publishes
    # the port to 127.0.0.1 on the host, where a reverse proxy terminates TLS.
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    api_token: str = ""
    # Exact origins the browser may call from. Never a wildcard.
    api_cors_origins: tuple[str, ...] = ()
    api_rate_limit_per_minute: int = 60
    # Seconds between start and READY before the process gives up and exits so
    # Docker restarts it. Guild chunking on a small server takes a few seconds.
    ready_timeout_seconds: int = 180
    # Text channel for dice rolls sent from the portal. The portal may also name
    # a channel per roll; either way it must be a channel of the configured guild.
    dice_channel_id: int | None = None

    music_enabled: bool = False
    music_r2_prefix: str = "music"
    music_default_volume: float = 0.3
    music_max_queue: int = 100
    music_cache_max_mb: int = 2000
    # A voice reconnect replaces the client and silently kills playback; this
    # restarts the current track where it left off.
    music_resume_after_reconnect: bool = True
    # yt-dlp breaks often and is off by default. It is also an optional import,
    # so a missing package is only ever an error for whoever turned this on.
    music_youtube_enabled: bool = False
    music_resolve_timeout_seconds: float = 20.0
    # Resolutions run as subprocesses; more than a couple at once on a 1-core
    # host starves the recording, and nobody queues that fast by hand.
    music_ytdlp_max_concurrent: int = 2
    # A YouTube stream URL is signed and short-lived. Re-resolve before playing
    # anything older than this rather than handing ffmpeg a dead link.
    music_stream_ttl_seconds: float = 1800.0
    # A misclicked 10-hour ambience video should not silently occupy the queue.
    music_max_track_seconds: float = 10800.0
    # Live streams never end, so they cannot be queued behind anything.
    music_allow_live: bool = False
    # Soundboard sounds from YouTube are downloaded once into the music cache and
    # played from disk, so a loop never meets an expired stream URL. These bound
    # what one link may cost this host.
    music_yt_sfx_max_seconds: float = 60.0
    music_yt_ambience_max_seconds: float = 1800.0
    music_yt_layer_max_mb: int = 40
    music_yt_download_timeout_seconds: float = 90.0
    # Uploads from the portal stream through this host's disk before R2, so
    # they are capped well below the raw-capture headroom.
    music_upload_max_mb: int = 150

    # Tunables that are not part of the documented .env surface but are still
    # kept out of the call sites so tests can override them.
    flush_interval_seconds: float = 5.0
    alone_grace_seconds: int = 45
    inbox_poll_seconds: int = 60
    cleanup_interval_seconds: int = 6 * 3600
    heartbeat_interval_seconds: int = 30
    voice_reconnect_attempts: int = 3

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def outbox_dir(self) -> Path:
        """Sessions staged for the transcriber to collect."""
        return self.data_dir / "outbox"

    @property
    def inbox_dir(self) -> Path:
        """Transcripts the transcriber has sent back."""
        return self.data_dir / "inbox"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def music_cache_dir(self) -> Path:
        """Tracks pulled from object storage, pruned back to a size cap.

        On the same disk as the raw capture, which is the point: the cache must
        lose to a recording that needs the space, not the other way round.
        """
        return self.data_dir / "music"

    @property
    def uses_r2(self) -> bool:
        return self.storage_backend == "r2"

    def ensure_dirs(self) -> None:
        """Create the data tree, with an actionable error if we cannot.

        The common failure is a bind-mounted host directory: Docker creates it
        owned by root, while the container deliberately runs as an unprivileged
        user, so the first write fails. A raw PermissionError traceback in a
        crash-looping container is a miserable way to learn that.
        """
        for path in (
            self.data_dir,
            self.sessions_dir,
            self.exports_dir,
            self.backups_dir,
            self.outbox_dir,
            self.inbox_dir,
            self.music_cache_dir,
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                uid = os.getuid() if hasattr(os, "getuid") else "?"
                raise ConfigError(
                    f"Cannot write to {path}. The data directory must be writable by the "
                    f"user this process runs as (uid {uid}). If you are using the bundled "
                    "docker-compose.yml, run this once on the host:\n"
                    "    mkdir -p data && sudo chown -R 10001:10001 data"
                ) from exc

        probe = self.data_dir / ".write-test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ConfigError(
                f"{self.data_dir} exists but is not writable: {exc}. On the host run:\n"
                "    sudo chown -R 10001:10001 data"
            ) from exc


def load_config() -> Config:
    """Build a Config from the process environment (loading .env if present)."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is optional at runtime
        pass

    guild_raw = _get("GUILD_ID", required=True)
    try:
        guild_id = int(guild_raw)
    except ValueError as exc:
        raise ConfigError(f"GUILD_ID must be a numeric snowflake, got {guild_raw!r}") from exc

    admin_raw = os.environ.get("ADMIN_USER_ID", "").strip()
    admin_user_id = int(admin_raw) if admin_raw else None

    dice_raw = os.environ.get("DICE_CHANNEL_ID", "").strip()
    try:
        dice_channel_id = int(dice_raw) if dice_raw else None
    except ValueError as exc:
        raise ConfigError("DICE_CHANNEL_ID must be a Discord channel id") from exc

    music_upload_max_mb = _get_int("MUSIC_UPLOAD_MAX_MB", 150)
    if not 1 <= music_upload_max_mb <= 1000:
        raise ConfigError("MUSIC_UPLOAD_MAX_MB must be between 1 and 1000.")

    ready_timeout_seconds = _get_int("READY_TIMEOUT_SECONDS", 180)
    if ready_timeout_seconds < 30:
        raise ConfigError("READY_TIMEOUT_SECONDS must be at least 30.")

    role_raw = os.environ.get("SESSION_ADMIN_ROLE_ID", "").strip()
    session_admin_role_id = int(role_raw) if role_raw else None

    audio_format = _get("AUDIO_FORMAT", "opus").lower()
    if audio_format not in {"opus", "wav", "mp3"}:
        raise ConfigError(f"AUDIO_FORMAT must be 'opus', 'wav' or 'mp3', got {audio_format!r}")

    timezone_name = _get("TIMEZONE", "Europe/Istanbul")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise ConfigError(f"Unknown TIMEZONE {timezone_name!r}") from exc

    storage_backend = _get("STORAGE_BACKEND", "local").lower()
    if storage_backend not in {"local", "r2"}:
        raise ConfigError(f"STORAGE_BACKEND must be 'local' or 'r2', got {storage_backend!r}")
    r2_settings = {
        "R2_ACCOUNT_ID": _get("R2_ACCOUNT_ID"),
        "R2_ACCESS_KEY_ID": _get("R2_ACCESS_KEY_ID"),
        "R2_SECRET_ACCESS_KEY": _get("R2_SECRET_ACCESS_KEY"),
        "R2_BUCKET": _get("R2_BUCKET"),
    }
    if storage_backend == "r2":
        # Failing here beats discovering it at the end of a four-hour session,
        # when the upload is the only thing standing between the audio and the
        # transcriber.
        missing = sorted(name for name, value in r2_settings.items() if not value)
        if missing:
            raise ConfigError(
                f"STORAGE_BACKEND=r2 needs {', '.join(missing)}. "
                "Create an R2 API token with Object Read & Write on your bucket."
            )

    api_enabled = _get_bool("API_ENABLED", False)
    api_token = _get("API_TOKEN")
    api_port = _get_int("API_PORT", 8080)
    if api_enabled:
        # This token is full control of recordings, including stopping a live
        # one. Refusing a weak or absent one at load is the only check we get.
        if not api_token:
            raise ConfigError(
                "API_ENABLED=true needs API_TOKEN. Generate one with:\n"
                '    python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if len(api_token) < 32:
            raise ConfigError("API_TOKEN must be at least 32 characters.")
        if not 1 <= api_port <= 65535:
            raise ConfigError(f"API_PORT must be between 1 and 65535, got {api_port}")

    cors_raw = _get("API_CORS_ORIGINS")
    api_cors_origins = tuple(part.strip() for part in cors_raw.split(",") if part.strip())
    if "*" in api_cors_origins:
        raise ConfigError(
            "API_CORS_ORIGINS cannot be '*'. List the portal's exact origin instead; "
            "a wildcard would let any page a browser visits drive this bot."
        )

    music_enabled = _get_bool("MUSIC_ENABLED", False)
    music_youtube_enabled = _get_bool("MUSIC_YTDLP_ENABLED", False)
    music_default_volume = _get_float("MUSIC_DEFAULT_VOLUME", 0.3)
    if not 0.0 <= music_default_volume <= 2.0:
        raise ConfigError(
            f"MUSIC_DEFAULT_VOLUME must be between 0 and 2, got {music_default_volume}"
        )
    if music_enabled and storage_backend != "r2" and not music_youtube_enabled:
        raise ConfigError(
            "MUSIC_ENABLED=true needs a source: either STORAGE_BACKEND=r2 (tracks "
            "under MUSIC_R2_PREFIX) or MUSIC_YTDLP_ENABLED=true."
        )

    return Config(
        discord_token=_get("DISCORD_TOKEN", required=True),
        guild_id=guild_id,
        transcribe_language=_get("TRANSCRIBE_LANGUAGE", "tr"),
        whisper_prompt_extra=_get("WHISPER_PROMPT_EXTRA", ""),
        data_dir=Path(_get("DATA_DIR", "/data")),
        audio_format=audio_format,
        audio_retention_days=_get_int("AUDIO_RETENTION_DAYS", 7),
        trash_retention_days=_get_int("TRASH_RETENTION_DAYS", 7),
        disk_warning_threshold_mb=_get_int("DISK_WARNING_THRESHOLD_MB", 2000),
        expected_session_hours=_get_float("EXPECTED_SESSION_HOURS", 4.0),
        admin_user_id=admin_user_id,
        session_admin_role_id=session_admin_role_id,
        export_max_discord_upload_mb=_get_int("EXPORT_MAX_DISCORD_UPLOAD_MB", 25),
        timezone_name=timezone_name,
        db_backup_keep_days=_get_int("DB_BACKUP_KEEP_DAYS", 14),
        outbox_enabled=_get_bool("OUTBOX_ENABLED", True),
        storage_backend=storage_backend,
        r2_account_id=r2_settings["R2_ACCOUNT_ID"],
        r2_access_key_id=r2_settings["R2_ACCESS_KEY_ID"],
        r2_secret_access_key=r2_settings["R2_SECRET_ACCESS_KEY"],
        r2_bucket=r2_settings["R2_BUCKET"],
        upload_interval_seconds=_get_int("UPLOAD_INTERVAL_SECONDS", 120),
        api_enabled=api_enabled,
        api_host=_get("API_HOST", "127.0.0.1"),
        api_port=api_port,
        api_token=api_token,
        api_cors_origins=api_cors_origins,
        api_rate_limit_per_minute=_get_int("API_RATE_LIMIT_PER_MINUTE", 60),
        ready_timeout_seconds=ready_timeout_seconds,
        dice_channel_id=dice_channel_id,
        music_enabled=music_enabled,
        music_r2_prefix=_get("MUSIC_R2_PREFIX", "music").strip("/"),
        music_default_volume=music_default_volume,
        music_max_queue=_get_int("MUSIC_MAX_QUEUE", 100),
        music_cache_max_mb=_get_int("MUSIC_CACHE_MAX_MB", 2000),
        music_resume_after_reconnect=_get_bool("MUSIC_RESUME_AFTER_RECONNECT", True),
        music_youtube_enabled=music_youtube_enabled,
        music_resolve_timeout_seconds=_get_float("MUSIC_YTDLP_TIMEOUT_SECONDS", 20.0),
        music_ytdlp_max_concurrent=_get_int("MUSIC_YTDLP_MAX_CONCURRENT", 2),
        music_stream_ttl_seconds=_get_float("MUSIC_STREAM_TTL_SECONDS", 1800.0),
        music_max_track_seconds=_get_float("MUSIC_MAX_TRACK_SECONDS", 10800.0),
        music_allow_live=_get_bool("MUSIC_ALLOW_LIVE", False),
        music_yt_sfx_max_seconds=_get_float("MUSIC_YT_SFX_MAX_SECONDS", 60.0),
        music_yt_ambience_max_seconds=_get_float("MUSIC_YT_AMBIENCE_MAX_SECONDS", 1800.0),
        music_yt_layer_max_mb=_get_int("MUSIC_YT_LAYER_MAX_MB", 40),
        music_yt_download_timeout_seconds=_get_float("MUSIC_YT_DOWNLOAD_TIMEOUT_SECONDS", 90.0),
        music_upload_max_mb=music_upload_max_mb,
    )
