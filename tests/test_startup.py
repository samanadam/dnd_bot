from __future__ import annotations

from types import SimpleNamespace

import pytest

from dnd_bot.startup import (
    READY_TIMEOUT_EXIT_CODE,
    start_until_stopped,
    supervise_ready,
    wait_until_ready,
)


async def test_wait_until_ready_true_when_ready_arrives():
    calls = {"n": 0}

    def is_ready():
        calls["n"] += 1
        return calls["n"] >= 3

    assert await wait_until_ready(is_ready, timeout=1.0, poll=0.01) is True


async def test_wait_until_ready_false_on_timeout():
    assert await wait_until_ready(lambda: False, timeout=0.05, poll=0.01) is False


async def test_start_until_stopped_swallows_closed_session_during_shutdown():
    async def start():
        raise RuntimeError("Session is closed")

    await start_until_stopped(start, is_shutting_down=lambda: True)


async def test_start_until_stopped_reraises_when_not_shutting_down():
    async def start():
        raise RuntimeError("Session is closed")

    with pytest.raises(RuntimeError):
        await start_until_stopped(start, is_shutting_down=lambda: False)


async def test_start_until_stopped_reraises_other_runtime_errors():
    async def start():
        raise RuntimeError("something else")

    with pytest.raises(RuntimeError, match="something else"):
        await start_until_stopped(start, is_shutting_down=lambda: True)


async def test_supervise_ready_closes_bot_and_reports_exit_code():
    closed = {"called": False}

    async def close():
        closed["called"] = True

    bot = SimpleNamespace(is_ready=lambda: False, close=close)
    assert await supervise_ready(bot, timeout=0.05, poll=0.01) == READY_TIMEOUT_EXIT_CODE
    assert closed["called"] is True


async def test_supervise_ready_returns_none_when_ready():
    bot = SimpleNamespace(is_ready=lambda: True, close=None)
    assert await supervise_ready(bot, timeout=0.05, poll=0.01) is None
