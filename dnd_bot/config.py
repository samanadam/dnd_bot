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

    return Config(
        discord_token=_get("DISCORD_TOKEN", required=True),
        guild_id=guild_id,
        transcribe_language=_get("TRANSCRIBE_LANGUAGE", "tr"),
        whisper_prompt_extra=_get("WHISPER_PROMPT_EXTRA", ""),
        data_dir=Path(_get("DATA_DIR", "/data")),
        audio_format=audio_format,
        audio_retention_days=_get_int("AUDIO_RETENTION_DAYS", 7),
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
    )
