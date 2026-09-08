"""Waiting for DAVE before the first packet arrives.

Discord's voice E2EE handshake finishes after the socket is up. Packets that
arrive before it does decrypt to garbage, so the recorder waits - but a channel
that never negotiates DAVE must not be made to wait for something that will
never happen.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from dnd_bot.recorder import _wait_for_dave


class FakeDave:
    def __init__(self, ready_after: int = 0) -> None:
        self._checks = 0
        self._ready_after = ready_after

    @property
    def ready(self) -> bool:
        self._checks += 1
        return self._checks > self._ready_after


class FakeState:
    def __init__(self, dave: object | None) -> None:
        self.dave_session = dave


class FakeVoiceClient:
    def __init__(self, dave: object | None) -> None:
        self._connection = FakeState(dave)


@pytest.mark.asyncio
async def test_returns_at_once_when_the_session_is_already_ready():
    await asyncio.wait_for(_wait_for_dave(FakeVoiceClient(FakeDave())), timeout=1.0)


@pytest.mark.asyncio
async def test_returns_at_once_when_the_channel_never_negotiates_dave():
    # Stage channels and non-E2EE connections have no session at all: waiting
    # ten seconds for one would delay every recording on them.
    await asyncio.wait_for(_wait_for_dave(FakeVoiceClient(None)), timeout=1.0)


@pytest.mark.asyncio
async def test_waits_until_the_session_becomes_ready():
    dave = FakeDave(ready_after=3)
    await asyncio.wait_for(_wait_for_dave(FakeVoiceClient(dave)), timeout=5.0)
    assert dave._checks > 3


@pytest.mark.asyncio
async def test_records_anyway_when_the_handshake_never_finishes(caplog):
    # Never blocking the session is the point: a stuck handshake degrades the
    # recording, but refusing to record loses it entirely.
    never = FakeDave(ready_after=10_000)
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(_wait_for_dave(FakeVoiceClient(never), timeout=0.3), timeout=5.0)
    assert "DAVE session was not ready" in caplog.text
