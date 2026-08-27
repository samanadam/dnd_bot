"""Environment-driven configuration. Nothing here is hardcoded at a call site."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
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


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_hhmm(value: str, name: str) -> dtime:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ConfigError(f"{name} must be HH:MM, got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        return dtime(hour=hour, minute=minute)
    except ValueError as exc:
        raise ConfigError(f"{name} must be HH:MM, got {value!r}") from exc


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int

    whisper_model: str = "medium"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    transcribe_language: str = "tr"
    whisper_beam_size: int = 5
    whisper_condition_on_previous_text: bool = False
    whisper_vad_min_silence_ms: int = 500
    whisper_prompt_extra: str = ""
    transcribe_chunk_minutes: int = 10
    filter_hallucinations: bool = True

    data_dir: Path = Path("/data")
    audio_format: str = "wav"
    audio_retention_days: int = 7
    disk_warning_threshold_mb: int = 2000
    admin_user_id: int | None = None
    export_max_discord_upload_mb: int = 25

    quiet_hours_enabled: bool = True
    quiet_hours_start: dtime = field(default_factory=lambda: dtime(0, 0))
    quiet_hours_end: dtime = field(default_factory=lambda: dtime(8, 0))
    timezone_name: str = "Europe/Istanbul"

    db_backup_keep_days: int = 14

    # Tunables that are not part of the documented .env surface but are still
    # kept out of the call sites so tests can override them.
    flush_interval_seconds: float = 5.0
    alone_grace_seconds: int = 45
    queue_poll_seconds: int = 180
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
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    def ensure_dirs(self) -> None:
        """Create the data tree, with an actionable error if we cannot.

        The common failure is a bind-mounted host directory: Docker creates it
        owned by root, while the container deliberately runs as an unprivileged
        user, so the first write fails. A raw PermissionError traceback in a
        crash-looping container is a miserable way to learn that.
        """
        for path in (self.data_dir, self.sessions_dir, self.exports_dir, self.backups_dir):
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

    audio_format = _get("AUDIO_FORMAT", "wav").lower()
    if audio_format not in {"wav", "mp3"}:
        raise ConfigError(f"AUDIO_FORMAT must be 'wav' or 'mp3', got {audio_format!r}")

    timezone_name = _get("TIMEZONE", "Europe/Istanbul")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise ConfigError(f"Unknown TIMEZONE {timezone_name!r}") from exc

    return Config(
        discord_token=_get("DISCORD_TOKEN", required=True),
        guild_id=guild_id,
        whisper_model=_get("WHISPER_MODEL", "medium"),
        whisper_device=_get("WHISPER_DEVICE", "cpu"),
        whisper_compute_type=_get("WHISPER_COMPUTE_TYPE", "int8"),
        transcribe_language=_get("TRANSCRIBE_LANGUAGE", "tr"),
        whisper_beam_size=_get_int("WHISPER_BEAM_SIZE", 5),
        whisper_condition_on_previous_text=_get_bool("WHISPER_CONDITION_ON_PREVIOUS_TEXT", False),
        whisper_vad_min_silence_ms=_get_int("WHISPER_VAD_MIN_SILENCE_MS", 500),
        whisper_prompt_extra=_get("WHISPER_PROMPT_EXTRA", ""),
        transcribe_chunk_minutes=_get_int("TRANSCRIBE_CHUNK_MINUTES", 10),
        filter_hallucinations=_get_bool("FILTER_HALLUCINATIONS", True),
        data_dir=Path(_get("DATA_DIR", "/data")),
        audio_format=audio_format,
        audio_retention_days=_get_int("AUDIO_RETENTION_DAYS", 7),
        disk_warning_threshold_mb=_get_int("DISK_WARNING_THRESHOLD_MB", 2000),
        admin_user_id=admin_user_id,
        export_max_discord_upload_mb=_get_int("EXPORT_MAX_DISCORD_UPLOAD_MB", 25),
        quiet_hours_enabled=_get_bool("QUIET_HOURS_ENABLED", True),
        quiet_hours_start=parse_hhmm(_get("QUIET_HOURS_START", "00:00"), "QUIET_HOURS_START"),
        quiet_hours_end=parse_hhmm(_get("QUIET_HOURS_END", "08:00"), "QUIET_HOURS_END"),
        timezone_name=timezone_name,
        db_backup_keep_days=_get_int("DB_BACKUP_KEEP_DAYS", 14),
    )
