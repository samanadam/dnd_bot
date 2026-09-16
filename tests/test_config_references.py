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

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "dnd_bot"
ENV_EXAMPLE = ROOT / ".env.example"

# Read by config.py but deliberately absent from .env.example: set by Docker,
# or read outside Config entirely.
UNDOCUMENTED_ENV: set[str] = set()

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


def env_names_read_by_config() -> set[str]:
    """Every literal env var name config.py reads, via os.environ or a _get*."""
    tree = ast.parse((PACKAGE / "config.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        func = node.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if called in {"_get", "_get_int", "_get_float", "_get_bool", "get"}:
            names.add(first.value)
    return names


def test_every_setting_is_documented_in_env_example():
    """A typo'd or undocumented env name is invisible until someone's game night.

    The scan above proves the code agrees with Config; this proves the operator
    can actually discover the setting.
    """
    documented = ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in env_names_read_by_config() - UNDOCUMENTED_ENV
        if f"\n{name}=" not in documented and f"# {name}=" not in documented
    )
    assert not missing, f".env.example does not mention: {', '.join(missing)}"


@pytest.mark.parametrize("module", python_modules(), ids=lambda p: p.name)
def test_every_config_attribute_referenced_exists(module: Path):
    valid = config_attribute_names()
    unknown = sorted(referenced_attributes(module) - valid)
    assert not unknown, (
        f"{module.name} reads config attributes that Config does not define: "
        f"{', '.join(unknown)}"
    )
