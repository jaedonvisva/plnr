import pytest

from discord_adapter.config import DiscordConfigError, load_config


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
