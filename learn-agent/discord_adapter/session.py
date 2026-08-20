"""AgentSession: the single owner of everything Playwright/LEARN-derived
for the life of the Discord bot process.

Everything that touches the authenticated LearnClient, the tool callables,
or run_turn() happens through this class, on the one worker thread its
executor owns -- Playwright's sync API is thread-affine, and this is what
lets discord_adapter/bot.py's asyncio event loop stay free to service
Discord heartbeats instead of blocking on a multi-second LLM+tool turn.
Nothing outside this class ever touches LearnClient, the tools dict, or a
Playwright object directly.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

from agent.backend import Backend
from agent.loop import run_turn
from agent.system_prompt import SYSTEM_PROMPT
from agent.usage import Usage
from learn.auth import BrowserClosedError

UNAVAILABLE_MESSAGE = "LEARN session unavailable -- restart the agent."


class SessionState(Enum):
    RUNNING = "running"
    BROWSER_FAILED = "browser_failed"


class AgentSession:
    def __init__(
        self,
        llm_client,
        backend: Backend,
        tools: dict[str, Callable],
        executor: ThreadPoolExecutor,
        run_turn_fn: Callable = run_turn,
        debug: bool = False,
    ):
        self._llm_client = llm_client
        self._backend = backend
        self._tools = tools
        self._executor = executor
        self._run_turn_fn = run_turn_fn
        self._debug = debug
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.usage = Usage()
        self.state = SessionState.RUNNING

    async def ask(self, text: str) -> str:
        """Run one full agent turn for `text` and return the reply. Never
        raises -- BrowserClosedError transitions state to BROWSER_FAILED
        (one-way; no auto-recovery, no second Playwright context) and
        the backend's own unavailable_error is reported back as a normal
        string reply, mirroring agent.py's REPL exception handling."""
        if self.state is SessionState.BROWSER_FAILED:
            return UNAVAILABLE_MESSAGE

        self.messages.append({"role": "user", "content": text})
        turn_usage = Usage()
        loop = asyncio.get_running_loop()

        try:
            reply = await loop.run_in_executor(
                self._executor,
                self._run_turn_fn,
                self._llm_client,
                self._backend.model,
                self.messages,
                self._tools,
                self._debug,
                turn_usage,
            )
        except BrowserClosedError:
            self.state = SessionState.BROWSER_FAILED
            return UNAVAILABLE_MESSAGE
        except self._backend.unavailable_error as exc:
            return f"{self._backend.name} is temporarily unavailable: {exc}. Try asking again."

        self.usage.add_usage(turn_usage)
        return reply

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.usage = Usage()

    def status(self) -> dict:
        return {
            "backend": self._backend.name,
            "model": self._backend.model,
            "state": self.state.value,
            "message_count": len(self.messages),
            "total_tokens": self.usage.total_tokens,
        }
