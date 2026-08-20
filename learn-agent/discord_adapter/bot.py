"""Discord transport for the LEARN agent -- authorization, commands, rate
limiting, and message chunking. Deliberately contains NO LEARN logic, no
Valence calls, no Playwright, no tool implementations, no LLM/prompt logic:
everything here either rejects a message outright or hands it to an
AgentSession (discord_adapter/session.py), which is the only thing that
ever touches run_turn()/TurnGuard/LearnClient/tools.

handle_incoming() is deliberately independent of discord.py's real
Message/Channel types so it can be unit tested without a real Discord
connection or bot token -- LearnDiscordClient (the actual discord.Client
subclass) is a thin wrapper that extracts the real fields and calls it.
"""

import asyncio

import discord

from discord_adapter.config import MAX_INCOMING_MESSAGE_LENGTH, DiscordConfig
from discord_adapter.ratelimit import RateLimiter

DISCORD_MESSAGE_LIMIT = 2000

TOO_LONG_MESSAGE_TEMPLATE = (
    "That message is too long ({length} characters, limit " f"{MAX_INCOMING_MESSAGE_LENGTH})."
)
RATE_LIMITED_MESSAGE = "You're sending messages too quickly. Please wait a moment and try again."
BUSY_MESSAGE = "Still working on your last question -- one moment."
RESET_CONFIRMATION = "Conversation history cleared."


def format_status(status: dict) -> str:
    """Non-sensitive status only -- never course data, tokens, or raw
    message content, per the design spec's explicit requirement."""
    state_emoji = "\U0001f7e2" if status["state"] == "running" else "\U0001f534"
    return (
        f"{state_emoji} LEARN Agent\n"
        f"Backend: {status['backend']} ({status['model']})\n"
        f"State: {status['state']}\n"
        f"Conversation: {status['message_count']} messages\n"
        f"Tokens used this session: {status['total_tokens']:,}"
    )


def chunk_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split text into <=limit-character chunks, preferring a paragraph
    break, then a word break, over a hard cut mid-word."""
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def handle_incoming(
    session,
    rate_limiter: RateLimiter,
    lock: asyncio.Lock,
    config: DiscordConfig,
    author_id: int,
    is_dm: bool,
    text: str,
) -> list[str]:
    """Core message-handling decision logic. Returns the message chunks to
    send in reply -- an empty list means send nothing (e.g. an
    unauthorized sender gets no confirmation the bot is even listening)."""
    if not is_dm or author_id != config.allowed_user_id:
        return []

    stripped = text.strip()

    if len(stripped) > MAX_INCOMING_MESSAGE_LENGTH:
        return [TOO_LONG_MESSAGE_TEMPLATE.format(length=len(stripped))]

    if not rate_limiter.allow():
        return [RATE_LIMITED_MESSAGE]

    lowered = stripped.lower()
    if lowered == "!reset":
        session.reset()
        return [RESET_CONFIRMATION]

    if lowered == "!status":
        return [format_status(session.status())]

    if lock.locked():
        return [BUSY_MESSAGE]

    async with lock:
        reply = await session.ask(stripped)
    return chunk_message(reply)


class LearnDiscordClient(discord.Client):
    """The actual discord.py connection. Constructed exactly once, in
    discord_bot.py's startup path, with an already-built AgentSession --
    this class never constructs one itself. Discord gateway
    reconnects (temporary internet loss, websocket disconnects) are
    handled internally by discord.py within this same Client instance and
    never recreate `self._session` -- the browser, LearnClient, and
    conversation history all survive a reconnect untouched."""

    def __init__(self, session, config: DiscordConfig, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)
        self._session = session
        self._config = config
        self._rate_limiter = RateLimiter(
            max_per_minute=config.max_messages_per_minute,
            max_per_hour=config.max_messages_per_hour,
        )
        self._lock = asyncio.Lock()

    async def on_ready(self) -> None:
        print(f"Discord bot connected as {self.user} -- listening for DMs.")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        replies = await handle_incoming(
            session=self._session,
            rate_limiter=self._rate_limiter,
            lock=self._lock,
            config=self._config,
            author_id=message.author.id,
            is_dm=is_dm,
            text=message.content,
        )
        for chunk in replies:
            await message.channel.send(chunk)
