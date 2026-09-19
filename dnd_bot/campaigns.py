"""Campaign rules that need no database: validation, prompt hints, corrections.

Kept free of I/O so the API, the slash commands and the transcript reader all
apply exactly the same limits and the same matching.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

NAME_MAX = 60
TERM_MAX = 60
TERMS_MAX = 100
CORRECTION_MAX = 80
CORRECTIONS_MAX = 200
HINT_CHARS = 300


def normalize_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("name must be a string.")
    name = raw.strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise ValueError(f"name must be 1-{NAME_MAX} characters.")
    return name


def normalize_terms(raw: object) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("terms must be a list of strings.")
    seen: set[str] = set()
    terms: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("every term must be a string.")
        term = item.strip()
        if not term:
            continue
        if len(term) > TERM_MAX:
            raise ValueError(f"a term may be at most {TERM_MAX} characters.")
        if term.casefold() in seen:
            continue
        seen.add(term.casefold())
        terms.append(term)
    if len(terms) > TERMS_MAX:
        raise ValueError(f"at most {TERMS_MAX} terms.")
    return terms


def normalize_corrections(raw: object) -> list[tuple[str, str]]:
    if not isinstance(raw, list):
        raise ValueError("corrections must be a list.")
    if len(raw) > CORRECTIONS_MAX:
        raise ValueError(f"at most {CORRECTIONS_MAX} corrections.")
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"heard", "correct"}:
            raise ValueError("each correction needs exactly 'heard' and 'correct'.")
        heard, correct = item["heard"], item["correct"]
        if not isinstance(heard, str) or not isinstance(correct, str):
            raise ValueError("'heard' and 'correct' must be strings.")
        heard, correct = heard.strip(), correct.strip()
        if not 1 <= len(heard) <= CORRECTION_MAX or not 1 <= len(correct) <= CORRECTION_MAX:
            raise ValueError(f"'heard' and 'correct' must be 1-{CORRECTION_MAX} characters.")
        if heard == correct or heard.casefold() in seen:
            continue
        seen.add(heard.casefold())
        pairs.append((heard, correct))
    return pairs


def prompt_hints(terms: Sequence[str], base: str = "", limit: int = HINT_CHARS) -> str:
    """The campaign's names as a Whisper prompt fragment, cut on whole terms."""
    kept: list[str] = []
    length = len("Names: ")
    for term in terms:
        length += len(term) + (2 if kept else 0)
        if length > limit:
            break
        kept.append(term)
    head = ("Names: " + ", ".join(kept) + (". " if base.strip() else "")) if kept else ""
    return (head + base.strip()).strip()


def apply_corrections(text: str, corrections: Sequence[tuple[str, str]]) -> str:
    """Whole-word, case-insensitive, single pass (a fix is never fixed again)."""
    if not text or not corrections:
        return text
    ordered = sorted(corrections, key=lambda pair: len(pair[0]), reverse=True)
    pattern = re.compile(
        "|".join(
            rf"(?P<g{index}>(?<!\w){re.escape(heard)}(?!\w))"
            for index, (heard, _) in enumerate(ordered)
        ),
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        return ordered[int(match.lastgroup[1:])][1]  # type: ignore[index]

    return pattern.sub(replace, text)


def relabel_map(
    participants: Mapping[str, str],
    base_labels: Mapping[str, str],
    characters: Mapping[str, str],
) -> dict[str, str]:
    """Who each speaker is called once a session belongs to a campaign.

    Character name first, then the plain nickname or username, then whatever
    was recorded (only for sessions from before base labels were stored).
    """
    return {
        user_id: characters.get(user_id) or base_labels.get(user_id) or recorded
        for user_id, recorded in participants.items()
    }
