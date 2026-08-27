"""Speaker label priority: character name > server nickname > username."""

from __future__ import annotations

from dnd_bot.labels import resolve_label, resolve_participants


def test_character_name_wins():
    assert resolve_label("10", {"10": "Thorin"}, "Ereno", "eren") == "Thorin"


def test_nickname_used_when_no_character():
    assert resolve_label("10", {}, "Ereno", "eren") == "Ereno"


def test_username_is_the_last_real_fallback():
    assert resolve_label("10", {}, None, "eren") == "eren"


def test_blank_values_do_not_win():
    assert resolve_label("10", {"10": "  "}, "  ", "eren") == "eren"


def test_completely_unknown_user_gets_a_stable_placeholder():
    assert resolve_label("10", {}, None, None) == "User 10"


def test_character_map_is_keyed_by_string_ids():
    assert resolve_label(10, {"10": "Thorin"}, None, "eren") == "Thorin"


def test_resolve_participants_applies_priority_per_member():
    members = [("10", "Ereno", "eren"), ("11", None, "ayse"), ("12", "Nick", "raw")]
    resolved = resolve_participants(members, {"12": "Elenya"})
    assert resolved == {"10": "Ereno", "11": "ayse", "12": "Elenya"}
