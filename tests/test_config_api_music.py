"""Config for the HTTP API and the music player.

The API is the first thing on this bot that is reachable from outside Discord,
so its settings fail at load rather than at request time: a short token or a
nonsense port should stop the process, the way a missing R2 credential already
does, instead of quietly exposing something.
"""

from __future__ import annotations

import pytest

from dnd_bot.config import Config, ConfigError, load_config

TOKEN = "t" * 32


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A minimal valid environment; tests add the keys they care about."""
    for name in list(dict(__import__("os").environ)):
        if name.startswith(("API_", "MUSIC_", "R2_", "STORAGE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("GUILD_ID", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return monkeypatch


# -- defaults ----------------------------------------------------------------


def test_api_and_music_are_off_by_default(env):
    config = load_config()
    assert config.api_enabled is False
    assert config.music_enabled is False
    assert config.music_youtube_enabled is False


def test_the_api_binds_loopback_by_default(env):
    """The container overrides this; the code default must never be 0.0.0.0."""
    assert load_config().api_host == "127.0.0.1"
    assert load_config().api_port == 8080


def test_music_cache_dir_sits_under_the_data_dir(config: Config):
    assert config.music_cache_dir == config.data_dir / "music"


def test_ensure_dirs_creates_the_music_cache(config: Config):
    config.ensure_dirs()
    assert config.music_cache_dir.is_dir()


# -- validation --------------------------------------------------------------


def test_an_enabled_api_demands_a_token(env):
    env.setenv("API_ENABLED", "true")
    with pytest.raises(ConfigError, match="API_TOKEN"):
        load_config()


def test_a_short_api_token_is_refused(env):
    env.setenv("API_ENABLED", "true")
    env.setenv("API_TOKEN", "short")
    with pytest.raises(ConfigError, match="32"):
        load_config()


def test_a_valid_api_token_loads(env):
    env.setenv("API_ENABLED", "true")
    env.setenv("API_TOKEN", TOKEN)
    config = load_config()
    assert config.api_enabled is True
    assert config.api_token == TOKEN


def test_a_nonsense_port_is_refused(env):
    env.setenv("API_ENABLED", "true")
    env.setenv("API_TOKEN", TOKEN)
    env.setenv("API_PORT", "70000")
    with pytest.raises(ConfigError, match="API_PORT"):
        load_config()


def test_cors_origins_split_on_commas_and_strip(env):
    env.setenv("API_CORS_ORIGINS", "https://a.example , https://b.example")
    assert load_config().api_cors_origins == ("https://a.example", "https://b.example")


def test_cors_origins_default_to_nothing_allowed(env):
    assert load_config().api_cors_origins == ()


def test_a_wildcard_cors_origin_is_refused(env):
    """`*` plus a bearer token that controls recordings is not a combination
    anyone should be able to configure by accident."""
    env.setenv("API_CORS_ORIGINS", "*")
    with pytest.raises(ConfigError, match="API_CORS_ORIGINS"):
        load_config()


def test_music_volume_must_be_in_range(env):
    env.setenv("MUSIC_DEFAULT_VOLUME", "5")
    with pytest.raises(ConfigError, match="MUSIC_DEFAULT_VOLUME"):
        load_config()


def test_music_needs_at_least_one_source(env):
    """R2 off and yt-dlp off means nothing could ever play."""
    env.setenv("MUSIC_ENABLED", "true")
    env.setenv("STORAGE_BACKEND", "local")
    with pytest.raises(ConfigError, match="MUSIC"):
        load_config()


def test_music_over_r2_loads(env):
    env.setenv("MUSIC_ENABLED", "true")
    env.setenv("STORAGE_BACKEND", "r2")
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        env.setenv(name, "set")
    config = load_config()
    assert config.music_enabled is True
    assert config.music_r2_prefix == "music"


def test_music_over_ytdlp_alone_loads(env):
    env.setenv("MUSIC_ENABLED", "true")
    env.setenv("MUSIC_YTDLP_ENABLED", "true")
    config = load_config()
    assert config.music_enabled is True
    assert config.music_youtube_enabled is True


def test_youtube_sound_limits_have_safe_defaults(env):
    config = load_config()
    assert config.music_yt_sfx_max_seconds == 60.0
    assert config.music_yt_ambience_max_seconds == 1800.0
    assert config.music_yt_layer_max_mb == 40
    assert config.music_yt_download_timeout_seconds == 90.0


def test_youtube_sound_limits_can_be_tuned(env):
    env.setenv("MUSIC_YT_SFX_MAX_SECONDS", "30")
    env.setenv("MUSIC_YT_AMBIENCE_MAX_SECONDS", "600")
    env.setenv("MUSIC_YT_LAYER_MAX_MB", "10")
    env.setenv("MUSIC_YT_DOWNLOAD_TIMEOUT_SECONDS", "45")
    config = load_config()
    assert (config.music_yt_sfx_max_seconds, config.music_yt_ambience_max_seconds) == (30.0, 600.0)
    assert (config.music_yt_layer_max_mb, config.music_yt_download_timeout_seconds) == (10, 45.0)
