"""Discord transport for the LEARN agent -- authorization, commands, rate
limiting, and message chunking. Deliberately contains NO LEARN logic, no
Valence calls, no browser, no tool implementations, no LLM/prompt logic:
everything here either rejects a message outright or hands it to an
AgentSession (discord_adapter/session.py), which is the only thing that
ever touches run_turn()/TurnGuard/LearnClient/tools.

handle_incoming() is deliberately independent of discord.py's real
Message/Channel types so it can be unit tested without a real Discord
connection or bot token -- LearnDiscordClient (the actual discord.Client
subclass) is a thin wrapper that extracts the real fields and calls it.
"""

import asyncio
import re

import discord

from discord_adapter.config import MAX_INCOMING_MESSAGE_LENGTH, DiscordConfig
from discord_adapter.ratelimit import RateLimiter

DISCORD_MESSAGE_LIMIT = 2000

_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def convert_markdown_tables(text: str) -> str:
    """Rewrite GitHub-flavored markdown tables (which Discord renders as
    raw pipe-delimited text, not an actual table) into padded, monospaced
    tables wrapped in a fenced code block. The LLM is instructed not to
    emit tables at all, but that's a request, not a guarantee -- this is
    the mechanical backstop that keeps tabular replies legible in Discord
    even when the model ignores it."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            "|" in line
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            header = _split_table_row(line)
            rows = []
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(_split_table_row(lines[j]))
                j += 1

            widths = [len(cell) for cell in header]
            for row in rows:
                for idx, cell in enumerate(row):
                    if idx >= len(widths):
                        widths.append(len(cell))
                    else:
                        widths[idx] = max(widths[idx], len(cell))

            def render(row: list[str]) -> str:
                padded = [
                    row[idx].ljust(widths[idx]) if idx < len(row) else " " * widths[idx]
                    for idx in range(len(widths))
                ]
                return "  ".join(padded).rstrip()

            table_lines = [render(header), "  ".join("-" * w for w in widths)]
            table_lines.extend(render(row) for row in rows)
            out.append("```")
            out.extend(table_lines)
            out.append("```")
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out)

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
    command_handler=None,
    reply_to_id: int | None = None,
) -> list[str]:
    """Core message-handling decision logic. Returns the message chunks to
    send in reply -- an empty list means send nothing (e.g. an
    unauthorized sender gets no confirmation the bot is even listening)."""
    if not is_dm or author_id != config.allowed_user_id:
        return []

    stripped = text.strip()

    if not stripped:
        return []

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

    # Replies to proactive messages ("done 2", "mute 2h", "!digest"): deterministic, no model.
    if command_handler is not None:
        command_reply = command_handler(stripped, reply_to_id)
        if command_reply is not None:
            return chunk_message(convert_markdown_tables(command_reply))

    if lock.locked():
        return [BUSY_MESSAGE]

    async with lock:
        reply = await session.ask(stripped)
    reply = convert_markdown_tables(reply)
    chunks = [c for c in chunk_message(reply) if c.strip()]
    return chunks or ["(no response)"]


class LearnDiscordClient(discord.Client):
    """The actual discord.py connection. Constructed exactly once, in
    discord_bot.py's startup path, with an already-built AgentSession --
    this class never constructs one itself. Discord gateway
    reconnects (temporary internet loss, websocket disconnects) are
    handled internally by discord.py within this same Client instance and
    never recreate `self._session` -- the LearnClient and
    conversation history all survive a reconnect untouched."""

    def __init__(self, session, config: DiscordConfig, proactive=None, **kwargs):
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
        # `proactive` is built in discord_bot.py (it needs LEARN access, which this module
        # must not have). Here it is only an object with run() and handle_command().
        self._proactive = proactive
        self._proactive_task: asyncio.Task | None = None

    async def on_ready(self) -> None:
        print(f"Discord bot connected as {self.user} -- listening for DMs.")
        # on_ready fires again after every gateway reconnect; start the loop exactly once.
        if self._proactive is not None and self._proactive_task is None:
            self._proactive_task = asyncio.create_task(self._proactive.run())
            print("Proactive monitoring on.")

    async def close(self) -> None:
        if self._proactive_task is not None:
            self._proactive_task.cancel()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_authorized = is_dm and message.author.id == self._config.allowed_user_id

        async def _handle() -> list[str]:
            return await handle_incoming(
                session=self._session,
                rate_limiter=self._rate_limiter,
                lock=self._lock,
                config=self._config,
                author_id=message.author.id,
                is_dm=is_dm,
                text=message.content,
                command_handler=self._proactive.handle_command if self._proactive else None,
                reply_to_id=message.reference.message_id if message.reference else None,
            )

        # Discord's native "X is typing..." indicator, so a long run_turn()
        # (multi-round tool calls) doesn't look like the bot went silent.
        # Only shown to the authorized sender -- an unauthorized DM still
        # gets zero visible reaction, matching handle_incoming()'s silent
        # rejection.
        if is_authorized:
            async with message.channel.typing():
                replies = await _handle()
        else:
            replies = await _handle()

        for chunk in replies:
            await message.channel.send(chunk)
