"""Adding and removing tracks in the music bucket from the portal.

An upload is the one request body here that is not a handful of JSON fields,
so it gets its own path through the API and its own checks, in this order:

1. the folder and file name are validated and rebuilt, never used as given;
2. the declared size is capped before a byte is read, and the stream is capped
   again while it is written, so a lying Content-Length gains nothing;
3. the bytes must start like the audio format their extension claims;
4. ffprobe must find an audio stream with a sane duration;
5. only then is the file put in R2, under a key that does not exist yet.

The file passes through this host's disk, which it shares with raw capture, so
one upload runs at a time and none starts when the disk is already tight.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .musiccache import CacheFull, cached_path, ensure_room
from .tracks import AUDIO_SUFFIXES

log = logging.getLogger(__name__)

# Folder name in the API -> key segment under the music prefix.
FOLDERS = {"music": "", "ambience": "ambience/", "sfx": "sfx/"}
SOUNDBOARD_FOLDERS = ("ambience", "sfx")
# A sound effect is a moment, not a track.
MAX_SFX_SECONDS = 120.0
MAX_NAME_LENGTH = 100
CHUNK_BYTES = 256 * 1024
PROBE_TIMEOUT_SECONDS = 30

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}

# Anything outside letters, digits, spaces and a little punctuation goes.
_UNSAFE = re.compile(r"[^\w .,()'&!+\-]", re.UNICODE)
_SPACES = re.compile(r"\s+")


class UploadError(Exception):
    """A refusal that is safe to show the caller."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Probe:
    has_audio: bool
    duration_seconds: float | None


def safe_filename(raw: str) -> str:
    """A file name that is safe as the last segment of an object key."""
    if not isinstance(raw, str):
        raise UploadError(400, "bad_filename", "A file name is required.")
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1].strip()
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise UploadError(400, "bad_filename", "The file name contains control characters.")
    stem, dot, extension = name.rpartition(".")
    suffix = f".{extension.lower()}"
    if not dot or suffix not in AUDIO_SUFFIXES:
        allowed = ", ".join(sorted(AUDIO_SUFFIXES))
        raise UploadError(415, "unsupported_format", f"Upload an audio file ({allowed}).")
    stem = _SPACES.sub(" ", _UNSAFE.sub("", stem)).strip(" .-")[:MAX_NAME_LENGTH].strip(" .-")
    if not stem:
        raise UploadError(400, "bad_filename", "The file name has no usable characters.")
    return f"{stem}{suffix}"


def object_key(prefix: str, folder: str, filename: str) -> str:
    if folder not in FOLDERS:
        raise UploadError(400, "bad_folder", "folder must be music, ambience or sfx.")
    return f"{prefix}/{FOLDERS[folder]}{safe_filename(filename)}"


def looks_like(head: bytes, suffix: str) -> bool:
    """Does the start of the file match the format its extension claims?"""
    id3 = head.startswith(b"ID3")
    mpeg_sync = len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
    if suffix == ".mp3":
        return id3 or mpeg_sync
    if suffix in {".ogg", ".opus"}:
        return head.startswith(b"OggS")
    if suffix == ".flac":
        return head.startswith(b"fLaC") or id3
    if suffix == ".wav":
        return head.startswith(b"RIFF") and head[8:12] == b"WAVE"
    if suffix == ".m4a":
        return head[4:8] == b"ftyp"
    if suffix == ".aac":
        return id3 or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0)
    return False


def probe_audio(path: Path) -> Probe:
    """ffprobe the file. Local files only; a missing ffprobe reads as "no audio"."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        f"file:{Path(path).resolve()}",
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return Probe(False, None)
    if result.returncode != 0:
        return Probe(False, None)
    try:
        info = json.loads(result.stdout or b"{}")
    except ValueError:
        return Probe(False, None)
    streams = info.get("streams") or []
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    try:
        duration = float((info.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return Probe(has_audio, duration)


def probe_duration(path: Path) -> float | None:
    probe = probe_audio(path)
    return probe.duration_seconds if probe.has_audio else None


class Uploader:
    """Owns the one-at-a-time rule and the temporary directory."""

    def __init__(self, config, store, source, prober=probe_audio) -> None:  # noqa: ANN001
        self.config = config
        self.store = store
        self.source = source
        self._prober = prober
        self._lock = asyncio.Lock()

    @property
    def staging_dir(self) -> Path:
        return self.config.data_dir / ".uploads"

    @property
    def max_bytes(self) -> int:
        return self.config.music_upload_max_mb * 1_000_000

    async def receive(
        self,
        stream: Any,
        *,
        folder: str,
        filename: str,
        content_length: int | None,
    ) -> dict[str, Any]:
        """Validate, stage and store one upload. `stream` has iter_chunked()."""
        if self.store is None or self.source is None:
            raise UploadError(503, "source_disabled", "Object storage is not configured.")
        key = object_key(self.config.music_r2_prefix, folder, filename)
        suffix = Path(key).suffix.lower()
        if content_length is None:
            raise UploadError(411, "length_required", "Send the file size (Content-Length).")
        if content_length <= 0:
            raise UploadError(400, "empty_file", "The file is empty.")
        if content_length > self.max_bytes:
            raise UploadError(
                413,
                "payload_too_large",
                f"Files can be at most {self.config.music_upload_max_mb} MB.",
            )
        if self._lock.locked():
            raise UploadError(409, "upload_busy", "Another upload is running. Try again shortly.")

        async with self._lock:
            if await self.source.contains(key):
                raise UploadError(409, "already_exists", "A file with that name already exists.")
            try:
                ensure_room(
                    self.config.music_cache_dir,
                    self.config.data_dir,
                    self.config.disk_warning_threshold_mb + self.config.music_upload_max_mb,
                )
            except CacheFull as exc:
                raise UploadError(
                    507, "insufficient_storage", "The bot's disk is too full for uploads."
                ) from exc

            self.staging_dir.mkdir(parents=True, exist_ok=True)
            staged = self.staging_dir / f"{secrets.token_hex(12)}{suffix}"
            try:
                written = await self._write(stream, staged)
                if written != content_length:
                    raise UploadError(400, "incomplete_upload", "The upload did not complete.")
                with staged.open("rb") as handle:
                    head = handle.read(16)
                if not looks_like(head, suffix):
                    raise UploadError(
                        415, "not_audio", f"That file is not a valid {suffix[1:]} file."
                    )
                probe = await asyncio.to_thread(self._prober, staged)
                if not probe.has_audio or not probe.duration_seconds:
                    raise UploadError(415, "not_audio", "No playable audio was found in that file.")
                limit = MAX_SFX_SECONDS if folder == "sfx" else self.config.music_max_track_seconds
                if probe.duration_seconds > limit:
                    raise UploadError(
                        413,
                        "too_long",
                        f"That file is too long for {folder} ({int(limit)} s at most).",
                    )
                await asyncio.to_thread(
                    self.store.client.upload_file,
                    Filename=str(staged),
                    Bucket=self.store.bucket,
                    Key=key,
                    ExtraArgs={
                        "ContentType": CONTENT_TYPES.get(suffix, "application/octet-stream")
                    },
                )
            finally:
                staged.unlink(missing_ok=True)

        self.source.invalidate()
        log.info("Uploaded %s (%d bytes) to the music bucket", key, content_length)
        return {
            "id": key,
            "title": Path(key).stem,
            "folder": folder,
            "size_bytes": content_length,
            "duration_seconds": round(probe.duration_seconds, 1),
        }

    async def _write(self, stream: Any, target: Path) -> int:
        written = 0
        with target.open("wb") as handle:
            async for chunk in stream.iter_chunked(CHUNK_BYTES):
                written += len(chunk)
                if written > self.max_bytes:
                    raise UploadError(
                        413,
                        "payload_too_large",
                        f"Files can be at most {self.config.music_upload_max_mb} MB.",
                    )
                await asyncio.to_thread(handle.write, chunk)
        return written

    async def delete(self, key: str) -> None:
        """Remove one track. The key must be a listed audio file under the prefix."""
        if self.store is None or self.source is None:
            raise UploadError(503, "source_disabled", "Object storage is not configured.")
        if not isinstance(key, str) or not key.startswith(self.source.prefix):
            raise UploadError(404, "not_found", "No such track.")
        if Path(key).suffix.lower() not in AUDIO_SUFFIXES or not await self.source.contains(key):
            raise UploadError(404, "not_found", "No such track.")
        await asyncio.to_thread(self.store.client.delete_object, Bucket=self.store.bucket, Key=key)
        cached_path(self.config.music_cache_dir, key).unlink(missing_ok=True)
        self.source.invalidate()
        log.info("Deleted %s from the music bucket", key)
