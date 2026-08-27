"""Whisper transcription and transcript rendering.

The merge/render half is pure and unit-tested; the model half is isolated behind
`WhisperTranscriber` so tests never need faster-whisper installed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .timeutil import format_duration, to_local

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawSegment:
    """A segment as Whisper reports it, relative to the start of one user's file."""

    start: float
    end: float
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0


@dataclass(frozen=True)
class Segment:
    """A segment placed on the shared session timeline."""

    speaker: str
    user_id: str
    start: float
    end: float
    text: str


class Transcriber(Protocol):
    """Anything that can turn an audio file into timestamped segments."""

    def transcribe_file(
        self, path: Path, language: str, initial_prompt: str | None = None
    ) -> list[RawSegment]: ...


# -- Turkish transcript quality -------------------------------------------

# Whisper was trained on a lot of subtitled video, so on near-silent or noisy
# stretches it emits subtitle boilerplate rather than nothing. These are the
# artifacts that actually show up on Turkish audio, plus the English ones that
# leak in. Matching is on a normalized form (lowercase, punctuation stripped).
HALLUCINATION_PHRASES: tuple[str, ...] = (
    "altyazi mk",
    "altyazi m k",
    "altyazi",
    "altyazilar",
    "altyazi ceviri",
    "abone olmayi unutmayin",
    "abone ol",
    "izlediginiz icin tesekkurler",
    "izlediginiz icin tesekkur ederim",
    "bizi takip etmeyi unutmayin",
    "kanalimiza abone olun",
    "bir sonraki videoda gorusmek uzere",
    "turkce altyazi",
    "thanks for watching",
    "thank you for watching",
    "thank you",
    "subtitles by the amaraorg community",
    "subscribe",
    "you",
)

_PUNCTUATION = str.maketrans({ch: " " for ch in ".,!?;:\"'()[]{}<>-–—_/\\|*#…♪♫"})
_TURKISH_FOLD = str.maketrans("çğıöşüâîûÇĞİÖŞÜ", "cgiosuaiuCGIOSU")


def normalize_for_match(text: str) -> str:
    """Lowercase, de-accent and squash whitespace, for artifact matching only."""
    folded = text.translate(_TURKISH_FOLD).lower()
    return " ".join(folded.translate(_PUNCTUATION).split())


def is_hallucination(text: str) -> bool:
    normalized = normalize_for_match(text)
    if not normalized:
        return True
    return normalized in HALLUCINATION_PHRASES


def filter_hallucinations(
    segments: Sequence[RawSegment],
    *,
    max_repeats: int = 3,
    min_avg_logprob: float = -1.0,
    max_no_speech_prob: float = 0.6,
) -> tuple[list[RawSegment], int]:
    """Drop subtitle boilerplate, low-confidence noise and stuck repeats.

    Per-speaker tracks are mostly silence (everyone else is talking), which is
    exactly the condition that makes Whisper hallucinate. Returns the kept
    segments and how many were dropped.
    """
    kept: list[RawSegment] = []
    dropped = 0
    previous_normalized = ""
    repeat_run = 0

    for segment in segments:
        normalized = normalize_for_match(segment.text)
        if is_hallucination(segment.text):
            dropped += 1
            continue
        # Low confidence AND likely silence - one alone is not enough to cut.
        if segment.avg_logprob < min_avg_logprob and segment.no_speech_prob > max_no_speech_prob:
            dropped += 1
            continue
        if normalized == previous_normalized:
            repeat_run += 1
            if repeat_run >= max_repeats:
                dropped += 1
                continue
        else:
            previous_normalized = normalized
            repeat_run = 0
        kept.append(segment)
    return kept, dropped


def build_initial_prompt(
    speaker_labels: Iterable[str],
    extra: str | None = None,
    *,
    max_chars: int = 600,
) -> str:
    """Prime Whisper with the session's vocabulary.

    Whisper leans on this for proper nouns, which is where a D&D transcript
    otherwise falls apart: character names are invented words that get mangled
    into whatever real Turkish word sounds closest. Naming them up front, plus
    saying the domain is Turkish D&D play, measurably improves both.
    """
    names = sorted({label.strip() for label in speaker_labels if label and label.strip()})
    parts = [
        "Bu bir Dungeons & Dragons masaustu rol yapma oyunu oturumunun kaydidir.",
        "Konusmalar Turkcedir; arada Ingilizce oyun terimleri gecer "
        "(dungeon master, saving throw, initiative, roll, level).",
    ]
    if names:
        parts.append("Karakterler ve oyuncular: " + ", ".join(names) + ".")
    if extra and extra.strip():
        parts.append(extra.strip())
    prompt = " ".join(parts)
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars].rsplit(" ", 1)[0]
    return prompt


# -- pure logic -----------------------------------------------------------


def merge_segments(
    per_user: Mapping[str, Sequence[RawSegment]],
    offsets: Mapping[str, float],
    labels: Mapping[str, str],
) -> list[Segment]:
    """Fold every speaker's segments onto one chronological timeline.

    Each user's file starts when that user's first packet arrived, so their
    segment timestamps are shifted by that user's offset from session start.
    Ties (overlapping speech) are ordered by end time then speaker name so the
    output is deterministic rather than dependent on dict iteration order.
    """
    merged: list[Segment] = []
    for user_id, segments in per_user.items():
        offset = float(offsets.get(user_id, 0.0))
        speaker = labels.get(user_id, f"User {user_id}")
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            merged.append(
                Segment(
                    speaker=speaker,
                    user_id=str(user_id),
                    start=round(segment.start + offset, 3),
                    end=round(segment.end + offset, 3),
                    text=text,
                )
            )
    merged.sort(key=lambda s: (s.start, s.end, s.speaker))
    return merged


def word_count(segments: Iterable[Segment]) -> int:
    return sum(len(segment.text.split()) for segment in segments)


def render_markdown(
    segments: Sequence[Segment],
    *,
    session_name: str,
    session_start: datetime,
    tz: ZoneInfo,
    duration_seconds: float,
    warnings: Sequence[str] = (),
) -> str:
    """`[HH:MM:SS] Speaker: line`, with timestamps in the configured local time."""
    local_start = to_local(session_start, tz)
    speakers = sorted({segment.speaker for segment in segments})
    lines = [
        f"# {session_name}",
        "",
        f"- **Date:** {local_start.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- **Duration:** {format_duration(duration_seconds)}",
        f"- **Speakers:** {len(speakers)} ({', '.join(speakers) if speakers else 'none'})",
        f"- **Words:** {word_count(segments)}",
    ]
    if warnings:
        lines.append("")
        lines.append("## Warnings")
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(["", "## Transcript", ""])

    if not segments:
        lines.append("_No speech was detected in this session._")
    else:
        for segment in segments:
            stamp = to_local(session_start + timedelta(seconds=segment.start), tz)
            lines.append(f"[{stamp.strftime('%H:%M:%S')}] {segment.speaker}: {segment.text}")
    return "\n".join(lines) + "\n"


def render_json(
    segments: Sequence[Segment],
    *,
    session_id: str,
    session_name: str,
    session_start: datetime,
    tz: ZoneInfo,
    duration_seconds: float,
    language: str,
    model: str,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "session_name": session_name,
        "start_time_utc": session_start.isoformat(),
        "timezone": str(tz),
        "duration_seconds": round(duration_seconds, 3),
        "language": language,
        "model": model,
        "speakers": sorted({segment.speaker for segment in segments}),
        "word_count": word_count(segments),
        "warnings": list(warnings),
        "segments": [
            {
                **asdict(segment),
                "start_local": to_local(
                    session_start + timedelta(seconds=segment.start), tz
                ).isoformat(),
            }
            for segment in segments
        ],
    }


def write_transcripts(
    md_path: Path, json_path: Path, markdown: str, payload: dict[str, Any]
) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# -- model wrapper --------------------------------------------------------


class WhisperTranscriber:
    """Lazily-loaded faster-whisper wrapper. One model instance per process."""

    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        compute_type: str = "int8",
        download_root: str | None = None,
        *,
        beam_size: int = 5,
        condition_on_previous_text: bool = False,
        vad_min_silence_ms: int = 500,
        filter_hallucinations_enabled: bool = True,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.beam_size = beam_size
        # Off by default: with one track per speaker, most of each file is
        # silence, and carrying context across those gaps is what sends Whisper
        # into repetition loops.
        self.condition_on_previous_text = condition_on_previous_text
        self.vad_min_silence_ms = vad_min_silence_ms
        self.filter_hallucinations_enabled = filter_hallucinations_enabled
        self._model: Any | None = None

    def load(self) -> Any:
        """Load the model, raising a clear error if it cannot be loaded at all."""
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info(
                "Loading Whisper model %s (device=%s, compute_type=%s)",
                self.model_name,
                self.device,
                self.compute_type,
            )
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.download_root,
            )
            log.info("Whisper model %s ready", self.model_name)
        return self._model

    def transcribe_file(
        self, path: Path, language: str, initial_prompt: str | None = None
    ) -> list[RawSegment]:
        model = self.load()
        segments, _info = model.transcribe(
            str(path),
            language=language,
            beam_size=self.beam_size,
            initial_prompt=initial_prompt,
            condition_on_previous_text=self.condition_on_previous_text,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": self.vad_min_silence_ms},
            # Retry a segment at higher temperature when greedy decoding comes
            # out badly, instead of emitting the garbage first attempt.
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            no_speech_threshold=0.6,
        )
        raw = [
            RawSegment(
                start=float(s.start),
                end=float(s.end),
                text=s.text,
                avg_logprob=float(getattr(s, "avg_logprob", 0.0) or 0.0),
                no_speech_prob=float(getattr(s, "no_speech_prob", 0.0) or 0.0),
            )
            for s in segments
            if (s.text or "").strip()
        ]
        if not self.filter_hallucinations_enabled:
            return raw
        kept, dropped = filter_hallucinations(raw)
        if dropped:
            log.info(
                "Dropped %s hallucinated/low-confidence segment(s) from %s", dropped, path.name
            )
        return kept
