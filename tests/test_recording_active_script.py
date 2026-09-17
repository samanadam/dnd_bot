"""The host autoheal script asks this whether restarting the bot is safe."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "recording_active.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("recording_active", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_urlopen(payload=None, error=None):
    def opener(request, timeout):
        if error:
            raise error
        assert request.full_url == "http://127.0.0.1:8080/api/v1/recording"
        assert request.get_header("Authorization") == "Bearer secret-token"
        return io.BytesIO(json.dumps(payload).encode())

    return opener


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("API_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")
    monkeypatch.delenv("API_PORT", raising=False)


def test_idle_bot_is_safe_to_restart(script, api_env, monkeypatch):
    monkeypatch.setattr(script.urllib.request, "urlopen", fake_urlopen([]))
    assert script.main() == 1


def test_active_recording_blocks_restart(script, api_env, monkeypatch):
    monkeypatch.setattr(script.urllib.request, "urlopen", fake_urlopen([{"session_id": "x"}]))
    assert script.main() == 0


def test_unreachable_api_counts_as_recording(script, api_env, monkeypatch):
    monkeypatch.setattr(script.urllib.request, "urlopen", fake_urlopen(error=OSError("down")))
    assert script.main() == 0


def test_api_disabled(script, monkeypatch):
    monkeypatch.setenv("API_ENABLED", "false")
    assert script.main() == 2
