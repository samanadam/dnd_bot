"""The interface between the recorder and the transcriber.

THIS FILE IS DUPLICATED VERBATIM IN THE TRANSCRIBER REPOSITORY.
Change it in one place and copy it to the other, in the same commit. The
`SCHEMA_VERSION` field exists so that a mismatch fails loudly instead of being
silently misread.

The two halves never talk to each other directly. They exchange directories:

    outbox/<session_id>/          recorder -> transcriber
        metadata.json
        <user_id>.<ext>           one track per speaker
        READY                     written last

    inbox/<session_id>/           transcriber -> recorder
        transcript.md
        transcript.json
        DONE                      written last

The marker files are the whole trick. They are written after everything else,
so a directory that is still being copied has no marker and is invisible to the
other side. An interrupted transfer is therefore harmless rather than a
corrupt half-session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

METADATA_FILENAME = "metadata.json"
READY_MARKER = "READY"
DONE_MARKER = "DONE"
TRANSCRIPT_MD = "transcript.md"
TRANSCRIPT_JSON = "transcript.json"


class ContractError(RuntimeError):
    """Raised when an exchanged directory cannot be trusted."""


@dataclass(frozen=True)
class SessionMetadata:
    """Everything the transcriber needs in order to work without a database.

    Speaker labels and offsets are resolved at record time and frozen here,
    which is what lets the transcriber run with no access to the recorder's
    SQLite database - or to Discord.
    """

    session_id: str
    name: str
    start_time_utc: str
    end_time_utc: str | None = None
    timezone: str = "UTC"
    channel_name: str | None = None
    participants: dict[str, str] = field(default_factory=dict)
    offsets: dict[str, float] = field(default_factory=dict)
    language: str = "tr"
    prompt_extra: str = ""
    audio_format: str = "opus"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "session_id": self.session_id,
            "name": self.name,
            "start_time_utc": self.start_time_utc,
            "end_time_utc": self.end_time_utc,
            "timezone": self.timezone,
            "channel_name": self.channel_name,
            "participants": dict(self.participants),
            "offsets": {key: float(value) for key, value in self.offsets.items()},
            "language": self.language,
            "prompt_extra": self.prompt_extra,
            "audio_format": self.audio_format,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SessionMetadata:
        schema = payload.get("schema")
        if schema != SCHEMA_VERSION:
            raise ContractError(
                f"Unsupported metadata schema {schema!r}; this build understands "
                f"{SCHEMA_VERSION}. Update both halves to the same version."
            )
        missing = [key for key in ("session_id", "name", "start_time_utc") if not payload.get(key)]
        if missing:
            raise ContractError(f"Metadata is missing required field(s): {', '.join(missing)}")
        return cls(
            session_id=str(payload["session_id"]),
            name=str(payload["name"]),
            start_time_utc=str(payload["start_time_utc"]),
            end_time_utc=payload.get("end_time_utc"),
            timezone=payload.get("timezone") or "UTC",
            channel_name=payload.get("channel_name"),
            participants={str(k): str(v) for k, v in (payload.get("participants") or {}).items()},
            offsets={str(k): float(v) for k, v in (payload.get("offsets") or {}).items()},
            language=payload.get("language") or "tr",
            prompt_extra=payload.get("prompt_extra") or "",
            audio_format=payload.get("audio_format") or "opus",
        )


# -- reading and writing --------------------------------------------------


def write_metadata(directory: Path, metadata: SessionMetadata) -> Path:
    path = Path(directory) / METADATA_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def read_metadata(directory: Path) -> SessionMetadata:
    path = Path(directory) / METADATA_FILENAME
    if not path.is_file():
        raise ContractError(f"No {METADATA_FILENAME} in {directory}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path} is not valid JSON: {exc}") from exc
    return SessionMetadata.from_dict(payload)


def mark(directory: Path, marker: str) -> Path:
    """Write a marker file. Always the last write into a directory."""
    path = Path(directory) / marker
    path.parent.mkdir(parents=True, exist_ok=True)
    # Flush to disk before the marker is visible to a poller on the other side.
    path.write_text("", encoding="utf-8")
    return path


def is_marked(directory: Path, marker: str) -> bool:
    return (Path(directory) / marker).is_file()


def ready_sessions(outbox_root: Path) -> list[Path]:
    """Session directories that are complete and safe to act on."""
    root = Path(outbox_root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and is_marked(p, READY_MARKER))


def done_sessions(inbox_root: Path) -> list[Path]:
    root = Path(inbox_root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and is_marked(p, DONE_MARKER))


def audio_tracks(session_dir: Path, audio_format: str) -> list[Path]:
    """Per-speaker tracks inside an exchanged session directory."""
    directory = Path(session_dir)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob(f"*.{audio_format}") if p.is_file() and not p.name.startswith(".")
    )


def validate_outbox(session_dir: Path) -> SessionMetadata:
    """Check a pulled session before spending an hour of CPU on it."""
    directory = Path(session_dir)
    if not is_marked(directory, READY_MARKER):
        raise ContractError(f"{directory} has no {READY_MARKER} marker; it may still be copying")
    metadata = read_metadata(directory)
    tracks = audio_tracks(directory, metadata.audio_format)
    if not tracks:
        raise ContractError(
            f"{directory} contains no .{metadata.audio_format} tracks to transcribe"
        )
    unknown = [p.stem for p in tracks if p.stem not in metadata.participants]
    if unknown:
        # Not fatal: a speaker with no label still gets a readable placeholder,
        # rather than the whole session being rejected.
        metadata = replace(
            metadata,
            participants={
                **metadata.participants,
                **{stem: f"User {stem}" for stem in unknown},
            },
        )
    return metadata
