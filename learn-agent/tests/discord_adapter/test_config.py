import pytest

from discord_adapter import config as config_module
from discord_adapter.config import DiscordConfigError, load_config


@pytest.fixture(autouse=True)
def _no_real_env_file(monkeypatch, tmp_path):
    """Every test in this file exercises presence/absence of specific env
    vars via monkeypatch.setenv/delenv. Without this fixture, a real .env
    on disk (e.g. a developer's own DISCORD_BOT_TOKEN/DISCORD_ALLOWED_USER_ID
    for actually running the bot) would get reloaded by
    _load_dotenv_if_present() and silently defeat delenv() -- python-dotenv
    repopulates any var that isn't currently set in the process environment.
    Pointing ENV_FILE at a path that doesn't exist makes these tests behave
    identically regardless of what's in the developer's real .env."""
    monkeypatch.setattr(config_module, "ENV_FILE", tmp_path / "nonexistent.env")


def test_load_config_reads_required_fields(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", "123456789")
    monkeypatch.delenv("DISCORD_MAX_MESSAGES_PER_MINUTE", raising=False)
    monkeypatch.delenv("DISCORD_MAX_MESSAGES_PER_HOUR", raising=False)

    config = load_config()

    assert config.bot_token == "fake-token"
    assert config.allowed_user_id == 123456789
    assert config.max_messages_per_minute == 5
    assert config.max_messages_per_hour == 50


def test_load_config_honors_rate_limit_overrides(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", "123456789")
    monkeypatch.setenv("DISCORD_MAX_MESSAGES_PER_MINUTE", "2")
    monkeypatch.setenv("DISCORD_MAX_MESSAGES_PER_HOUR", "10")

    config = load_config()

    assert config.max_messages_per_minute == 2
    assert config.max_messages_per_hour == 10


def test_load_config_raises_when_token_missing(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", "123456789")

    with pytest.raises(DiscordConfigError):
        load_config()


def test_load_config_raises_when_allowed_user_id_missing(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("DISCORD_ALLOWED_USER_ID", raising=False)

    with pytest.raises(DiscordConfigError):
        load_config()


def test_load_config_raises_when_allowed_user_id_not_an_integer(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", "not-a-number")

    with pytest.raises(DiscordConfigError):
        load_config()


def test_load_config_raises_when_max_messages_per_minute_not_an_integer(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", "123456789")
    monkeypatch.setenv("DISCORD_MAX_MESSAGES_PER_MINUTE", "not-a-number")

    with pytest.raises(DiscordConfigError):
        load_config()
