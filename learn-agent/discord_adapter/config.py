"""Discord adapter configuration, read from the same .env file the rest of
the project already uses (see learn/credentials.py, agent/gemini_client.py
for the identical load-dotenv-if-present pattern).

DISCORD_BOT_TOKEN and DISCORD_ALLOWED_USER_ID are required -- there is no
sensible default for either, and starting the bot without them would mean
either no bot connects at all, or (far worse) a bot that would answer
anyone who DMs it. Both failing loudly at startup is intentional.
"""

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_MAX_MESSAGES_PER_MINUTE = 5
DEFAULT_MAX_MESSAGES_PER_HOUR = 50

MAX_INCOMING_MESSAGE_LENGTH = 4000
"""Caps how large a single incoming DM can be before it's even passed to
run_turn()/Gemini -- defense-in-depth against someone pasting an enormous
payload into the bot, independent of TurnGuard's per-turn tool-call
limits (agent/limits.py), which this doesn't touch or replace."""


class DiscordConfigError(Exception):
    """Raised when required Discord configuration is missing or invalid."""


@dataclass
class DiscordConfig:
    bot_token: str
    allowed_user_id: int
    max_messages_per_minute: int
    max_messages_per_hour: int


def _load_dotenv_if_present() -> None:
    if not ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ENV_FILE)


def load_config() -> DiscordConfig:
    _load_dotenv_if_present()

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise DiscordConfigError("DISCORD_BOT_TOKEN is not set. Add it to your .env file.")

    raw_user_id = os.environ.get("DISCORD_ALLOWED_USER_ID")
    if not raw_user_id:
        raise DiscordConfigError("DISCORD_ALLOWED_USER_ID is not set. Add it to your .env file.")
    try:
        allowed_user_id = int(raw_user_id)
    except ValueError:
        raise DiscordConfigError(
            f"DISCORD_ALLOWED_USER_ID must be an integer, got: {raw_user_id!r}"
        ) from None

    raw_max_per_minute = os.environ.get(
        "DISCORD_MAX_MESSAGES_PER_MINUTE", DEFAULT_MAX_MESSAGES_PER_MINUTE
    )
    try:
        max_per_minute = int(raw_max_per_minute)
    except ValueError:
        raise DiscordConfigError(
            f"DISCORD_MAX_MESSAGES_PER_MINUTE must be an integer, got: {raw_max_per_minute!r}"
        ) from None

    raw_max_per_hour = os.environ.get(
        "DISCORD_MAX_MESSAGES_PER_HOUR", DEFAULT_MAX_MESSAGES_PER_HOUR
    )
    try:
        max_per_hour = int(raw_max_per_hour)
    except ValueError:
        raise DiscordConfigError(
            f"DISCORD_MAX_MESSAGES_PER_HOUR must be an integer, got: {raw_max_per_hour!r}"
        ) from None

    return DiscordConfig(
        bot_token=token,
        allowed_user_id=allowed_user_id,
        max_messages_per_minute=max_per_minute,
        max_messages_per_hour=max_per_hour,
    )
