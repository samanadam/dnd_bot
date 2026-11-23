import pytest

from dnd_bot.api.middleware import ApiError
from dnd_bot.api.routes_partynotes import _author, _text, format_note


def test_author_is_escaped_and_text_is_posted_as_written():
    assert format_note("Mira*", "meet at dawn") == r"**Mira\*:** meet at dawn"


@pytest.mark.parametrize("value", ["", "   ", "x" * 1501, 5])
def test_text_must_be_1_to_1500_characters(value):
    with pytest.raises(ApiError):
        _text({"text": value})


def test_author_is_required():
    with pytest.raises(ApiError):
        _author({})
    assert _author({"author": "  Mira  "}) == "Mira"
