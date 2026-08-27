"""Running one transcription job end to end.

Kept free of Discord imports so it can be exercised with a fake transcriber.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import paths
from .chunking import CHUNK_DIR_NAME, cleanup_chunks, split_wav
from .timeutil import from_iso, utcnow
from .transcription import (
    RawSegment,
    Segment,
    Transcriber,
    build_initial_prompt,
    merge_segments,
    render_json,
    render_markdown,
    word_count,
    write_transcripts,
)

log = logging.getLogger(__name__)


class TranscriptionJobError(RuntimeError):
    """Raised when a job cannot produce a transcript at all."""


@dataclass
class JobResult:
    session_id: str
    transcript_md: Path
    transcript_json: Path
    duration_seconds: float
    speaker_count: int
    word_count: int
    segment_count: int
    warnings: list[str] = field(default_factory=list)


def audio_files_for(session_dir_audio: Path, audio_format: str) -> list[Path]:
    if not session_dir_audio.is_dir():
        return []
    return sorted(p for p in session_dir_audio.glob(f"*.{audio_format}") if p.is_file())


def session_duration_seconds(session: dict[str, Any]) -> float:
    start = from_iso(session.get("start_time"))
    end = from_iso(session.get("end_time")) or utcnow()
    if start is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def transcribe_one(
    path: Path,
    transcriber: Transcriber,
    language: str,
    initial_prompt: str | None,
    chunk_seconds: float,
) -> list[RawSegment]:
    """Transcribe one speaker track, in chunks, with timestamps re-based.

    Chunking is what keeps peak memory flat: faster-whisper loads whatever file
    it is given entirely into memory first, so a whole four-hour track would
    need several GB before decoding even starts.
    """
    try:
        chunks = split_wav(path, path.parent / CHUNK_DIR_NAME, chunk_seconds)
    except Exception as exc:  # noqa: BLE001 - e.g. AUDIO_FORMAT=mp3, which `wave` cannot read
        log.warning(
            "Could not split %s (%s); transcribing it whole. Long sessions in this "
            "format may use a lot of memory.",
            path.name,
            exc,
        )
        return transcriber.transcribe_file(path, language, initial_prompt)

    if len(chunks) == 1 and chunks[0].path == path:
        return transcriber.transcribe_file(path, language, initial_prompt)

    segments: list[RawSegment] = []
    try:
        for index, chunk in enumerate(chunks, start=1):
            log.info("Transcribing %s chunk %s/%s", path.name, index, len(chunks))
            for segment in transcriber.transcribe_file(chunk.path, language, initial_prompt):
                segments.append(
                    RawSegment(
                        start=segment.start + chunk.offset,
                        end=segment.end + chunk.offset,
                        text=segment.text,
                        avg_logprob=segment.avg_logprob,
                        no_speech_prob=segment.no_speech_prob,
                    )
                )
            # Free each chunk as soon as it is done, not at the end.
            chunk.path.unlink(missing_ok=True)
    finally:
        cleanup_chunks(chunks, path)
    return segments


def transcribe_all(
    audio_paths: list[Path],
    transcriber: Transcriber,
    language: str,
    initial_prompt: str | None = None,
    chunk_seconds: float = 600.0,
) -> tuple[dict[str, list[RawSegment]], list[str]]:
    """Transcribe every speaker file, skipping (not failing on) unreadable ones."""
    per_user: dict[str, list[RawSegment]] = {}
    warnings: list[str] = []
    for path in audio_paths:
        user_id = path.stem
        try:
            per_user[user_id] = transcribe_one(
                path, transcriber, language, initial_prompt, chunk_seconds
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not sink the job
            log.exception("Transcription failed for %s", path)
            warnings.append(f"Skipped speaker {user_id}: {type(exc).__name__}: {exc}")
    if audio_paths and not per_user:
        raise TranscriptionJobError(
            "Every speaker file failed to transcribe: " + "; ".join(warnings)
        )
    return per_user, warnings


def build_transcript(
    session: dict[str, Any],
    per_user: dict[str, list[RawSegment]],
    *,
    tz: ZoneInfo,
    warnings: list[str],
    sessions_root: Path,
) -> JobResult:
    """Merge, render and write both transcript files for a session."""
    session_id = session["id"]
    labels: dict[str, str] = json.loads(session.get("participants_json") or "{}")
    offsets: dict[str, float] = {
        key: float(value) for key, value in json.loads(session.get("offsets_json") or "{}").items()
    }
    segments: list[Segment] = merge_segments(per_user, offsets, labels)
    duration = session_duration_seconds(session)
    name = session.get("name") or f"Session {session_id[:8]}"
    start = from_iso(session["start_time"]) or utcnow()

    markdown = render_markdown(
        segments,
        session_name=name,
        session_start=start,
        tz=tz,
        duration_seconds=duration,
        warnings=warnings,
    )
    payload = render_json(
        segments,
        session_id=session_id,
        session_name=name,
        session_start=start,
        tz=tz,
        duration_seconds=duration,
        language=session.get("language") or "",
        model=session.get("model_used") or "",
        warnings=warnings,
    )
    md_path = paths.transcript_md_path(sessions_root, session_id)
    json_path = paths.transcript_json_path(sessions_root, session_id)
    write_transcripts(md_path, json_path, markdown, payload)

    return JobResult(
        session_id=session_id,
        transcript_md=md_path,
        transcript_json=json_path,
        duration_seconds=duration,
        speaker_count=len({segment.speaker for segment in segments}),
        word_count=word_count(segments),
        segment_count=len(segments),
        warnings=warnings,
    )


async def run_job(
    session: dict[str, Any],
    transcriber: Transcriber,
    *,
    sessions_root: Path,
    audio_format: str,
    language: str,
    tz: ZoneInfo,
    prompt_extra: str | None = None,
    chunk_seconds: float = 600.0,
) -> JobResult:
    """Transcribe a finished session. Whisper runs in a worker thread."""
    session_id = session["id"]
    audio_dir = paths.audio_dir(sessions_root, session_id)
    audio_paths = audio_files_for(audio_dir, audio_format)
    warnings: list[str] = []

    # Feed this session's character names to Whisper so proper nouns survive.
    labels: dict[str, str] = json.loads(session.get("participants_json") or "{}")
    initial_prompt = build_initial_prompt(labels.values(), prompt_extra)

    if not audio_paths:
        warnings.append("No finalized audio files were found for this session.")
        per_user: dict[str, list[RawSegment]] = {}
    else:
        per_user, warnings = await asyncio.to_thread(
            transcribe_all, audio_paths, transcriber, language, initial_prompt, chunk_seconds
        )

    return await asyncio.to_thread(
        build_transcript,
        session,
        per_user,
        tz=tz,
        warnings=warnings,
        sessions_root=sessions_root,
    )
