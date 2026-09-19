"""Reading a delivered transcript back for the portal.

The transcriber writes two files per session. `transcript.json` is preferred:
it has exact timings. Older sessions, or a transcriber that only produced the
Markdown, fall back to parsing `transcript.md`'s `[HH:MM:SS] Speaker: text`
lines.

What leaves here is deliberately narrower than what is on disk: the JSON's
segments carry each speaker's Discord user id, and those never go into an API
response. Speakers are labels only.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .campaigns import apply_corrections

MAX_SEGMENT_CHARS = 5000
MAX_LABEL_CHARS = 100
# Parsed transcripts kept in memory, keyed by path and mtime.
CACHE_ENTRIES = 4

_MD_LINE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] ([^:\n]{1,100}): (.*)$")
_MD_HEADER = re.compile(r"^- \*\*(Duration|Speakers|Words|Date):\*\* (.*)$")


class TranscriptMissing(LookupError):
    """No transcript has been delivered for this session."""


@dataclass
class Transcript:
    meta: dict[str, Any]
    segments: list[dict[str, Any]]
    # Discord user id of each segment's speaker, parallel to `segments`. Kept
    # off the segments themselves so an id can never reach a response by way of
    # a spread; empty for transcripts parsed from Markdown, which has none.
    user_ids: list[str] = field(default_factory=list)


def _text(value: object, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(float(value), 2)


def _clock(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def from_json(raw: dict[str, Any]) -> Transcript:
    segments = []
    user_ids = []
    for item in raw.get("segments") or []:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), MAX_SEGMENT_CHARS)
        if not text:
            continue
        start = _seconds(item.get("start"))
        user_ids.append(_text(item.get("user_id"), 40))
        segments.append(
            {
                "speaker": _text(item.get("speaker"), MAX_LABEL_CHARS) or "Unknown",
                "start": start,
                "end": _seconds(item.get("end")),
                "clock": _clock(start),
                "text": text,
            }
        )
    speakers = sorted({segment["speaker"] for segment in segments})
    meta = {
        "language": _text(raw.get("language"), 16) or None,
        "timezone": _text(raw.get("timezone"), 64) or None,
        "duration_seconds": _seconds(raw.get("duration_seconds")),
        "word_count": sum(len(segment["text"].split()) for segment in segments),
        "speakers": speakers,
        "warnings": [
            _text(warning, 300) for warning in (raw.get("warnings") or [])[:20] if warning
        ],
    }
    return Transcript(meta, segments, user_ids)


def from_markdown(text: str) -> Transcript:
    """Markdown fallback. Clock times are local wall-clock, not offsets."""
    segments = []
    in_body = False
    for line in text.splitlines():
        if line.startswith("## Transcript"):
            in_body = True
            continue
        if not in_body:
            continue
        match = _MD_LINE.match(line)
        if match:
            hours, minutes, secs, speaker, body = match.groups()
            body = body.strip()[:MAX_SEGMENT_CHARS]
            if body:
                segments.append(
                    {
                        "speaker": speaker.strip() or "Unknown",
                        "start": None,
                        "end": None,
                        "clock": f"{hours}:{minutes}:{secs}",
                        "text": body,
                    }
                )
        elif segments and line.strip() and not line.startswith("["):
            # A wrapped continuation of the previous line.
            last = segments[-1]
            last["text"] = f"{last['text']} {line.strip()}"[:MAX_SEGMENT_CHARS]
    speakers = sorted({segment["speaker"] for segment in segments})
    meta = {
        "language": None,
        "timezone": None,
        "duration_seconds": None,
        "word_count": sum(len(segment["text"].split()) for segment in segments),
        "speakers": speakers,
        "warnings": [],
    }
    return Transcript(meta, segments)


class TranscriptReader:
    def __init__(self, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)
        self._cache: OrderedDict[tuple[str, int], Transcript] = OrderedDict()

    def available(self, session_id: str) -> bool:
        return (
            paths.transcript_json_path(self.sessions_root, session_id).is_file()
            or paths.transcript_md_path(self.sessions_root, session_id).is_file()
        )

    def read(
        self,
        session_id: str,
        *,
        relabel: Mapping[str, str] | None = None,
        corrections: Sequence[tuple[str, str]] = (),
    ) -> Transcript:
        """Blocking: run it in a thread.

        The cache holds the transcript as the transcriber wrote it. A campaign's
        relabel map and word corrections are applied on the way out, on a copy,
        and never stored - so one campaign's view cannot leak into another's.
        """
        transcript = self._load(session_id)
        if not relabel and not corrections:
            return transcript
        segments = []
        for index, segment in enumerate(transcript.segments):
            user_id = transcript.user_ids[index] if index < len(transcript.user_ids) else ""
            segments.append(
                {
                    **segment,
                    "speaker": (relabel or {}).get(user_id, segment["speaker"]),
                    "text": apply_corrections(segment["text"], corrections),
                }
            )
        return Transcript(
            {
                **transcript.meta,
                "speakers": sorted({segment["speaker"] for segment in segments}),
                "word_count": sum(len(segment["text"].split()) for segment in segments),
            },
            segments,
            list(transcript.user_ids),
        )

    def _load(self, session_id: str) -> Transcript:
        json_path = paths.transcript_json_path(self.sessions_root, session_id)
        md_path = paths.transcript_md_path(self.sessions_root, session_id)
        path = json_path if json_path.is_file() else md_path
        if not path.is_file():
            raise TranscriptMissing(session_id)

        key = (str(path), path.stat().st_mtime_ns)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        text = path.read_text(encoding="utf-8", errors="replace")
        transcript = None
        if path == json_path:
            try:
                raw = json.loads(text)
                if isinstance(raw, dict):
                    transcript = from_json(raw)
            except ValueError:
                transcript = None
            if transcript is None and md_path.is_file():
                transcript = from_markdown(md_path.read_text(encoding="utf-8", errors="replace"))
        else:
            transcript = from_markdown(text)
        if transcript is None:
            raise TranscriptMissing(session_id)

        self._cache[key] = transcript
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return transcript
