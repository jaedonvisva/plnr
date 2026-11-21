"""Sends the proactive agent's messages as Discord DMs to the one authorized user.

Kept apart from bot.py's inbound handling. Mentions are disabled on everything sent, so
instructor-written text quoted in a ping can never @-ping anyone.
"""

import discord

from discord_adapter.bot import chunk_message


class DiscordNotifier:
    def __init__(self, user_id: int):
        self._client: discord.Client | None = None
        self._user_id = user_id

    def bind(self, client: discord.Client) -> None:
        """The Discord client and the proactive agent need each other; wire the client in last."""
        self._client = client

    async def send(self, text: str) -> list[int]:
        if self._client is None:
            raise RuntimeError("DiscordNotifier used before bind()")
        user = await self._client.fetch_user(self._user_id)
        ids = []
        for chunk in chunk_message(text):
            sent = await user.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            ids.append(sent.id)
        return ids
