"""DnDBot must not shadow any attribute discord.Client keeps for itself.

This is here because it happened. `DnDBot.__init__` set `self._tasks = []` for
its own background loops, and `discord.Client.__init__` uses that exact name for
the set of internal tasks it calls `.add()` on. Our assignment ran after
`super().__init__()`, so it replaced the set with a list, and the bot crashed
with `AttributeError: 'list' object has no attribute 'add'` the moment py-cord
scheduled anything - after authenticating, so the logs looked like a successful
login followed by a mystery.

Nothing caught it: every test drove SessionManager directly and never built a
DnDBot. So rather than assert one name, this compares everything we assign
against everything the library assigns, and fails on any overlap.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import discord
import pytest

BOT_SOURCE = Path(__file__).resolve().parent.parent / "dnd_bot" / "bot.py"


def library_attributes() -> set[str]:
    """Everything py-cord's Bot and its bases assign to self in __init__.

    Read from the source rather than by constructing a Bot: instantiating one
    touches the event loop, which makes the result depend on whatever async
    test ran before this one.
    """
    found: set[str] = set()
    for klass in discord.Bot.__mro__:
        if klass is object or not klass.__module__.startswith("discord"):
            continue
        try:
            source = Path(inspect.getfile(klass))
        except TypeError:  # pragma: no cover - builtins have no file
            continue
        found |= attributes_assigned_in_init(source, klass.__name__)
    return found


def attributes_assigned_in_init(source: Path, class_name: str) -> set[str]:
    """`self.<name> = ...` inside that class's __init__."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assigned: set[str] = set()
    for klass in ast.walk(tree):
        if not isinstance(klass, ast.ClassDef) or klass.name != class_name:
            continue
        for node in klass.body:
            if not isinstance(node, ast.FunctionDef) or node.name != "__init__":
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign):
                    targets = inner.targets
                elif isinstance(inner, ast.AnnAssign):
                    targets = [inner.target]
                else:
                    continue
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        assigned.add(target.attr)
    return assigned


def test_the_scanner_finds_our_attributes():
    """Guard the guard: an empty scan would make the check below meaningless."""
    ours = attributes_assigned_in_init(BOT_SOURCE, "DnDBot")
    assert {"config", "db", "manager"} <= ours


def test_the_library_exposes_the_names_we_must_avoid():
    attrs = library_attributes()
    assert "_tasks" in attrs, "py-cord still owns _tasks; this check is still needed"


def test_dndbot_shadows_nothing_the_library_owns():
    ours = attributes_assigned_in_init(BOT_SOURCE, "DnDBot")
    collisions = sorted(ours & library_attributes())
    assert not collisions, (
        "DnDBot.__init__ overwrites attribute(s) discord.Client sets on itself: "
        f"{', '.join(collisions)}. Rename ours - the assignment runs after "
        "super().__init__(), so the library's value is destroyed."
    )


@pytest.mark.parametrize("name", ["_tasks", "loop", "ws", "http", "shard_id"])
def test_specific_library_internals_are_left_alone(name: str):
    assert name not in attributes_assigned_in_init(BOT_SOURCE, "DnDBot")
