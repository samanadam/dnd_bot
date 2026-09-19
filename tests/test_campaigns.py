from __future__ import annotations

import pytest

from dnd_bot.campaigns import (
    apply_corrections,
    normalize_corrections,
    normalize_name,
    normalize_terms,
    prompt_hints,
    relabel_map,
)


def test_name_is_trimmed_and_bounded():
    assert normalize_name("  Strahd  ") == "Strahd"
    with pytest.raises(ValueError):
        normalize_name("   ")
    with pytest.raises(ValueError):
        normalize_name("x" * 61)
    with pytest.raises(ValueError):
        normalize_name(5)


def test_terms_are_deduplicated_and_validated():
    assert normalize_terms([" Eldrin ", "eldrin", "Neverwinter", ""]) == ["Eldrin", "Neverwinter"]
    with pytest.raises(ValueError):
        normalize_terms("Eldrin")
    with pytest.raises(ValueError):
        normalize_terms(["x" * 61])
    with pytest.raises(ValueError):
        normalize_terms([f"t{i}" for i in range(101)])


def test_corrections_are_validated():
    assert normalize_corrections([{"heard": " el drin ", "correct": "Eldrin"}]) == [
        ("el drin", "Eldrin")
    ]
    assert normalize_corrections([{"heard": "a", "correct": "a"}]) == []
    with pytest.raises(ValueError):
        normalize_corrections([{"heard": "", "correct": "x"}])
    with pytest.raises(ValueError):
        normalize_corrections([{"heard": "a"}])
    with pytest.raises(ValueError):
        normalize_corrections([{"heard": "a", "correct": "b", "extra": 1}])
    with pytest.raises(ValueError):
        normalize_corrections([{"heard": f"h{i}", "correct": "c"} for i in range(201)])


def test_prompt_hints_cut_on_whole_terms():
    hints = prompt_hints(["Eldrin", "Neverwinter", "Zhentarim"], limit=26)
    assert hints == "Names: Eldrin, Neverwinter"
    assert prompt_hints([], base="global") == "global"
    assert prompt_hints(["A"], base="global") == "Names: A. global"
    assert prompt_hints([]) == ""


def test_corrections_match_whole_words_case_insensitively():
    pairs = [("el drin", "Eldrin"), ("drin", "Drin")]
    assert apply_corrections("Ben EL DRIN ile geldim", pairs) == "Ben Eldrin ile geldim"
    assert apply_corrections("drinking", pairs) == "drinking"
    assert apply_corrections("no match here", pairs) == "no match here"
    assert apply_corrections("", pairs) == ""


def test_corrections_do_not_cascade():
    pairs = [("a", "b"), ("b", "c")]
    assert apply_corrections("a b", pairs) == "b c"


def test_corrections_handle_turkish_dotted_capital():
    assert apply_corrections("İ x", [("i", "I")]) in {"I x", "İ x"}  # must not raise


def test_relabel_prefers_campaign_character_then_base_then_recorded():
    participants = {"10": "Old Character", "11": "Recorded", "12": "Gone"}
    base = {"10": "PlayerNick", "11": "Elenya"}
    characters = {"10": "Thorin of B"}
    assert relabel_map(participants, base, characters) == {
        "10": "Thorin of B",
        "11": "Elenya",
        "12": "Gone",
    }
