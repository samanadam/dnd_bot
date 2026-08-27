from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dnd_bot.config import Config  # noqa: E402
from dnd_bot.timeutil import to_iso  # noqa: E402

SESSION_START = datetime(2026, 5, 1, 18, 0, 0, tzinfo=UTC)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        discord_token="test-token",
        guild_id=1,
        data_dir=tmp_path / "data",
        audio_retention_days=7,
        disk_warning_threshold_mb=1,
        admin_user_id=999,
        queue_poll_seconds=1,
    )


@pytest.fixture
def session_row() -> dict:
    return {
        "id": "session-1",
        "name": "Test Session",
        "guild_id": "1",
        "channel_id": "2",
        "channel_name": "Table",
        "text_channel_id": "3",
        "started_by_user_id": "10",
        "start_time": to_iso(SESSION_START),
        "end_time": to_iso(SESSION_START.replace(hour=20)),
        "completed": 1,
        "transcribed": 0,
        "cancelled": 0,
        "audio_expires_at": None,
        "participants_json": '{"10": "Thorin", "11": "Elenya"}',
        "offsets_json": '{"10": 0.0, "11": 12.5}',
        "language": "tr",
        "model_used": "medium",
    }
