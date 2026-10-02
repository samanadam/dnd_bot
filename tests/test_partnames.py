"""Parts of a split game are named so they sort and read together."""

from __future__ import annotations

from dnd_bot.partnames import MAX_NAME, base_name, part_name


def test_a_plain_name_gets_a_part_suffix():
    assert part_name("Kamp Gecesi", 2) == "Kamp Gecesi (part 2)"


def test_an_existing_suffix_is_replaced_not_stacked():
    assert part_name("Kamp Gecesi (part 1)", 2) == "Kamp Gecesi (part 2)"
    assert base_name("Kamp Gecesi (Part 3)") == "Kamp Gecesi"


def test_only_a_trailing_suffix_counts():
    assert base_name("The (part 2) heist") == "The (part 2) heist"


def test_an_empty_name_falls_back_to_session():
    assert part_name("", 1) == "Session (part 1)"
    assert part_name("(part 4)", 5) == "Session (part 5)"


def test_a_long_name_is_trimmed_but_keeps_its_part():
    name = part_name("x" * 300, 12)
    assert len(name) <= MAX_NAME
    assert name.endswith(" (part 12)")
