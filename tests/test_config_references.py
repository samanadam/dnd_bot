"""Every `config.<name>` in the package must actually exist on Config.

This exists because it did not. Splitting the transcriber out removed
`whisper_model` from Config but left `self.config.whisper_model` in the recorder,
so `/session start` connected to the voice channel and then raised
AttributeError - the bot could not record at all, and no unit test noticed
because none of them drove the full start path.

A static check is the cheap way to catch the whole class: a typo or a removed
setting fails here instead of at the top of somebody's game night.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from dnd_bot.config import Config

PACKAGE = Path(__file__).resolve().parent.parent / "dnd_bot"

# Names that are legitimately config-shaped but are not Config attributes:
# locals called `config` that hold something else entirely.
IGNORED_MODULES: set[str] = set()


def config_attribute_names() -> set[str]:
    """Anything reachable on a Config: fields, properties and methods."""
    fields = {f.name for f in dataclasses.fields(Config)}
    return fields | {name for name in dir(Config) if not name.startswith("__")}


def referenced_attributes(path: Path) -> set[str]:
    """Every `X.config.<attr>` and `config.<attr>` read in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        # self.config.<attr> / cls.config.<attr>
        if isinstance(base, ast.Attribute) and base.attr == "config":
            found.add(node.attr)
        # config.<attr>
        elif isinstance(base, ast.Name) and base.id == "config":
            found.add(node.attr)
    return found


def python_modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if p.name != "__init__.py")


def test_the_scanner_finds_something_at_all():
    """Guard the guard: a silently empty scan would pass every check below."""
    all_referenced: set[str] = set()
    for module in python_modules():
        all_referenced |= referenced_attributes(module)
    assert "data_dir" in all_referenced
    assert len(all_referenced) > 5


@pytest.mark.parametrize("module", python_modules(), ids=lambda p: p.name)
def test_every_config_attribute_referenced_exists(module: Path):
    valid = config_attribute_names()
    unknown = sorted(referenced_attributes(module) - valid)
    assert not unknown, (
        f"{module.name} reads config attributes that Config does not define: "
        f"{', '.join(unknown)}"
    )
