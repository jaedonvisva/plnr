# Discord DM Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Discord DM transport layer to the existing LEARN agent, so `python discord_bot.py` authenticates against LEARN exactly as `agent.py` does today, then answers your DM questions via the existing, unmodified `run_turn()` path.

**Architecture:** A new `discord_adapter/` package (sibling of `agent/` and `learn/`) owns everything Discord-specific. `AgentSession` (in `discord_adapter/session.py`) is the sole owner of a single-worker thread pool that both launches the browser/auth and runs every `run_turn()` call, so Playwright's thread-affine sync API is never violated while `discord.py`'s asyncio event loop stays free to service heartbeats. `discord_adapter/bot.py` is pure transport (auth, commands, rate limiting, chunking) and never imports `learn.*` or `agent.tools`/`agent.course_registry`. A small, behavior-preserving extraction in `agent.py` gives both the CLI and the Discord entry point one shared "connect LLM, launch browser, authenticate, build client/tools" function instead of duplicating it.

**Tech Stack:** Python 3.14, `discord.py`, existing `playwright`/`ollama`/`google-genai`/`python-dotenv`/`pytest` stack (see `learn-agent/requirements.txt`).

**Spec:** `docs/superpowers/specs/2026-08-19-discord-adapter-design.md`

## Global Constraints

- Do not modify `LearnClient`, `learn/browser.py`, `learn/auth.py`, `learn/credentials.py`, `CourseRegistry`, `TurnGuard`, `agent/tools.py`, `agent/serialize.py`, `agent/system_prompt.py`, `agent/loop.py`'s `run_turn()` semantics, or `agent/gemini_client.py`/`agent/llm.py`.
- No alternate tool-execution path — every answer goes through `run_turn()` → `TurnGuard` → whitelisted tool → `CourseRegistry`/validation → `LearnClient`, unmodified.
- No macOS-specific code anywhere in `discord_adapter/` or `discord_bot.py`.
- Single authorized Discord user (by ID), DMs only, in this milestone.
- `discord_adapter/bot.py` must never import `learn.*`, `agent.tools`, or `agent.course_registry`.
- `AgentSession` is constructed exactly once, in `discord_bot.py`'s startup path — never inside `discord_adapter/bot.py`.
- All work happens in `/path/to/plnr/learn-agent/` (the existing project root — where `agent.py`, `agent/`, `learn/`, `tests/`, `requirements.txt`, `.env.example`, and `conftest.py` already live). New docs (this plan, the spec) live under `/path/to/plnr/docs/`.
- Run all commands from `/path/to/plnr/learn-agent/` with the project's venv active (`source .venv/bin/activate`), matching the existing test setup.
- Commit after every task, following the existing repo's plain (no `Co-Authored-By`) commit style seen in its first commit.

---

## File Structure

New:
- `discord_adapter/__init__.py` — empty, makes the package importable (mirrors `agent/__init__.py`/`learn/__init__.py`).
- `discord_adapter/config.py` — `DiscordConfig` dataclass, `load_config()`, `DiscordConfigError`.
- `discord_adapter/ratelimit.py` — `RateLimiter` (sliding-window, injectable clock).
- `discord_adapter/session.py` — `AgentSession`, `SessionState`.
- `discord_adapter/bot.py` — `handle_incoming()` (pure, discord.py-independent core logic), `chunk_message()`, `format_status()`, `LearnDiscordClient` (the actual `discord.Client` subclass).
- `discord_bot.py` (top level, sibling of `agent.py`) — entry point.
- `tests/discord_adapter/__init__.py`
- `tests/discord_adapter/test_config.py`
- `tests/discord_adapter/test_ratelimit.py`
- `tests/discord_adapter/test_session.py`
- `tests/discord_adapter/test_bot.py`
- `tests/discord_adapter/test_import_boundaries.py`

Modified:
- `agent.py` — extract `connect_and_authenticate()` and `AgentStartupError` out of `run_interactive_agent()`; `run_interactive_agent()` calls the new function instead of inlining the browser/auth block. No behavior change.
- `requirements.txt` — add `discord.py`.
- `.env.example` — add `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USER_ID`, and commented-out `DISCORD_MAX_MESSAGES_PER_MINUTE`/`DISCORD_MAX_MESSAGES_PER_HOUR`.

---

### Task 1: Extract `connect_and_authenticate()` out of `agent.py`

**Files:**
- Modify: `learn-agent/agent.py:90-124` (the browser-launch/auth block inside `run_interactive_agent()`)
- Test: existing suite (`learn-agent/tests/`) — this task adds no new automated tests (the extracted function launches a real, visible browser and can't run offline), but must not break any existing test.

**Interfaces:**
- Produces: `agent.AgentStartupError` (exception), `agent.connect_and_authenticate(backend: backend_module.Backend) -> tuple[BrowserContext, Playwright, LearnClient, dict[str, Callable]]` — on success returns the four objects, caller owns `context`/`playwright` cleanup. On failure, raises `AgentStartupError` after already closing `context` and stopping `playwright` itself.

- [ ] **Step 1: Read the current `run_interactive_agent()` body to confirm line numbers haven't drifted**

Run: `sed -n '90,166p' agent.py` (from `learn-agent/`)
Expected: matches the block quoted below (browser launch through the `while True` REPL loop and its `finally`).

- [ ] **Step 2: Add `AgentStartupError` and `connect_and_authenticate()`, and rewrite `run_interactive_agent()` to use it**

Replace the full body of `run_interactive_agent()` (currently `agent.py:90-165`) with:

```python
class AgentStartupError(Exception):
    """Raised by connect_and_authenticate() if LEARN authentication doesn't
    complete -- either the browser was closed early, or the wait timed out.
    The browser context is already closed and playwright already stopped
    before this is raised, so the caller has nothing left to clean up."""


def connect_and_authenticate(
    backend: backend_module.Backend,
) -> tuple[object, object, LearnClient, dict]:
    """Launch the browser, complete WatIAM/Duo auth (with optional
    credential auto-fill), and build a ready-to-use LearnClient + tool set.

    Returns (context, playwright, learn_client, tools). On success, the
    CALLER owns `context`/`playwright` and is responsible for calling
    context.close() and playwright.stop() when done (mirrors
    learn/browser.py::launch_browser()'s existing contract). On failure,
    raises AgentStartupError, having already closed the browser and
    stopped playwright itself -- shared by run_interactive_agent() (the
    CLI) and discord_bot.py, so this security-relevant wiring exists in
    exactly one place.
    """
    print("\nOpening LEARN...")
    context, playwright = browser.launch_browser()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(browser.LEARN_URL)

        if credentials.maybe_autofill_login(page):
            print("Filled WatIAM credentials from .env.")
            print("If Duo challenges you, approve it in the browser.")
        else:
            print("Please complete the Waterloo login and Duo authentication")
            print("in the browser.")

        try:
            authenticated = auth.wait_for_authentication(page)
        except auth.BrowserClosedError as exc:
            raise AgentStartupError(str(exc)) from exc

        if not authenticated:
            raise AgentStartupError("Timed out waiting for LEARN authentication.")

        print("LEARN authentication ✓\n")
    except AgentStartupError:
        try:
            context.close()
        except Exception:
            pass
        playwright.stop()
        raise

    client = LearnClient(context)
    tools = build_tools(client)
    return context, playwright, client, tools


def run_interactive_agent(backend: backend_module.Backend, debug: bool = False) -> None:
    """Full interactive agent, using the existing auth flow unmodified."""
    print("LEARN Agent")
    print("───────────")

    llm_client = _connect(backend)

    try:
        context, playwright, client, tools = connect_and_authenticate(backend)
    except AgentStartupError as exc:
        print(f"\n{exc}")
        return

    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        session_usage = Usage()

        print("Ask about your courses, assignments, grades, quizzes, or announcements.")
        print("Type 'exit' to quit.\n")

        while True:
            try:
                user_input = input("You: ").strip()
            except EOFError:
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                print("Goodbye.")
                break

            messages.append({"role": "user", "content": user_input})
            turn_usage = Usage()
            try:
                reply = run_turn(llm_client, backend.model, messages, tools, debug=debug, usage=turn_usage)
            except auth.BrowserClosedError:
                print("\nThe browser was closed. Restart the agent to continue.")
                break
            except backend.unavailable_error as exc:
                print(f"\n{backend.name} is temporarily unavailable: {exc}")
                print("Try asking again.\n")
                continue
            session_usage.add_usage(turn_usage)
            print(f"\nAgent:\n{reply}\n")
            if turn_usage.total_tokens:
                print(_format_usage(turn_usage, session_usage))
    finally:
        try:
            context.close()
        except Exception:
            pass
        playwright.stop()
```

- [ ] **Step 3: Run the full existing test suite to confirm nothing broke**

Run: `python -m pytest` (from `learn-agent/`, venv active)
Expected: all tests pass, same count as before this change (84 as of the README, may differ slightly if the repo has moved on — the point is zero new failures).

- [ ] **Step 4: Manual smoke-test note (cannot be automated)**

This extraction changes real control flow around a live browser/auth session. Before moving on, run `python agent.py` once by hand and confirm: browser opens, you can complete WatIAM/Duo, the REPL starts, one question gets answered, `exit` cleanly closes the browser. This mirrors the README's existing "you must re-verify behaviorally, live" policy for auth-adjacent changes.

- [ ] **Step 5: Commit**

```bash
git add agent.py
git commit -m "refactor: extract connect_and_authenticate() from run_interactive_agent()"
```

---

### Task 2: `discord_adapter/config.py`

**Files:**
- Create: `learn-agent/discord_adapter/__init__.py` (empty)
- Create: `learn-agent/discord_adapter/config.py`
- Test: `learn-agent/tests/discord_adapter/__init__.py` (empty), `learn-agent/tests/discord_adapter/test_config.py`

**Interfaces:**
- Produces: `discord_adapter.config.DiscordConfig` (dataclass: `bot_token: str`, `allowed_user_id: int`, `max_messages_per_minute: int`, `max_messages_per_hour: int`), `discord_adapter.config.DiscordConfigError` (exception), `discord_adapter.config.load_config() -> DiscordConfig`, `discord_adapter.config.MAX_INCOMING_MESSAGE_LENGTH` (int constant, `4000`).

- [ ] **Step 1: Create the empty package files**

```bash
mkdir -p discord_adapter tests/discord_adapter
touch discord_adapter/__init__.py tests/discord_adapter/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/discord_adapter/test_config.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/discord_adapter/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discord_adapter.config'`

- [ ] **Step 3: Write `discord_adapter/config.py`**

```python
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

    max_per_minute = int(
        os.environ.get("DISCORD_MAX_MESSAGES_PER_MINUTE", DEFAULT_MAX_MESSAGES_PER_MINUTE)
    )
    max_per_hour = int(
        os.environ.get("DISCORD_MAX_MESSAGES_PER_HOUR", DEFAULT_MAX_MESSAGES_PER_HOUR)
    )

    return DiscordConfig(
        bot_token=token,
        allowed_user_id=allowed_user_id,
        max_messages_per_minute=max_per_minute,
        max_messages_per_hour=max_per_hour,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/discord_adapter/test_config.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add discord_adapter/__init__.py discord_adapter/config.py tests/discord_adapter/__init__.py tests/discord_adapter/test_config.py
git commit -m "feat: add discord_adapter config loading"
```

---

### Task 3: `discord_adapter/ratelimit.py`

**Files:**
- Create: `learn-agent/discord_adapter/ratelimit.py`
- Test: `learn-agent/tests/discord_adapter/test_ratelimit.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `discord_adapter.ratelimit.RateLimiter(max_per_minute: int, max_per_hour: int, clock: Callable[[], float] = time.monotonic)` with `.allow() -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/discord_adapter/test_ratelimit.py`:

```python
from discord_adapter.ratelimit import RateLimiter


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_per_minute_limit():
    clock = FakeClock()
    limiter = RateLimiter(max_per_minute=3, max_per_hour=100, clock=clock)

    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False


def test_per_minute_limit_resets_after_60_seconds():
    clock = FakeClock()
    limiter = RateLimiter(max_per_minute=1, max_per_hour=100, clock=clock)

    assert limiter.allow() is True
    assert limiter.allow() is False

    clock.advance(61)
    assert limiter.allow() is True


def test_allows_up_to_per_hour_limit_even_with_minute_gaps():
    clock = FakeClock()
    limiter = RateLimiter(max_per_minute=1, max_per_hour=2, clock=clock)

    assert limiter.allow() is True
    clock.advance(61)
    assert limiter.allow() is True
    clock.advance(61)
    assert limiter.allow() is False


def test_per_hour_limit_resets_after_3600_seconds():
    clock = FakeClock()
    limiter = RateLimiter(max_per_minute=100, max_per_hour=1, clock=clock)

    assert limiter.allow() is True
    assert limiter.allow() is False

    clock.advance(3601)
    assert limiter.allow() is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/discord_adapter/test_ratelimit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discord_adapter.ratelimit'`

- [ ] **Step 3: Write `discord_adapter/ratelimit.py`**

```python
"""Persistent, cross-message rate limiting for the Discord adapter.

Deliberately separate from agent/limits.py's TurnGuard: TurnGuard bounds
tool-call abuse WITHIN one turn (defense-in-depth against a runaway or
prompt-injected tool loop). This bounds request volume ACROSS many turns
over minutes/hours, for an always-listening transport where "who's asking"
and "how many times have they asked recently" now actually exist -- exactly
the layer TurnGuard's own module docstring said would need new plumbing
once a real caller with a notion of sender showed up. Not primarily about
the single authorized user abusing their own bot -- it's a safety net if
the bot token leaks or the Discord account is compromised.
"""

import time
from collections.abc import Callable


class RateLimiter:
    """Sliding-window limiter over the last minute and the last hour.
    `clock` is injectable so tests don't need to sleep for real."""

    def __init__(
        self,
        max_per_minute: int,
        max_per_hour: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max_per_minute = max_per_minute
        self._max_per_hour = max_per_hour
        self._clock = clock
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        now = self._clock()
        self._timestamps = [t for t in self._timestamps if now - t < 3600]

        per_minute_count = sum(1 for t in self._timestamps if now - t < 60)
        if per_minute_count >= self._max_per_minute:
            return False
        if len(self._timestamps) >= self._max_per_hour:
            return False

        self._timestamps.append(now)
        return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/discord_adapter/test_ratelimit.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add discord_adapter/ratelimit.py tests/discord_adapter/test_ratelimit.py
git commit -m "feat: add persistent rate limiter for the Discord adapter"
```

---

### Task 4: `discord_adapter/session.py` (`AgentSession`)

**Files:**
- Create: `learn-agent/discord_adapter/session.py`
- Test: `learn-agent/tests/discord_adapter/test_session.py`

**Interfaces:**
- Consumes: `agent.loop.run_turn(ollama_client, model, messages, tools, debug=False, usage=None) -> str` (signature only — real function used as the default `run_turn_fn`, tests inject a fake with the same positional shape); `agent.usage.Usage` (`.total_tokens`, `.add_usage(other)`); `agent.system_prompt.SYSTEM_PROMPT` (str); `agent.backend.Backend` (`.name: str`, `.model: str`, `.unavailable_error: type[Exception]`); `learn.auth.BrowserClosedError`.
- Produces: `discord_adapter.session.SessionState` (`Enum`: `RUNNING`, `BROWSER_FAILED`), `discord_adapter.session.AgentSession(llm_client, backend, tools, executor, run_turn_fn=run_turn, debug=False)` with `async def ask(self, text: str) -> str`, `def reset(self) -> None`, `def status(self) -> dict`, and public attributes `.messages: list[dict]`, `.usage: Usage`, `.state: SessionState`. `discord_adapter.session.UNAVAILABLE_MESSAGE` (str constant).

- [ ] **Step 1: Write the failing tests**

Create `tests/discord_adapter/test_session.py`:

```python
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.backend import Backend
from agent.usage import Usage
from discord_adapter.session import AgentSession, SessionState, UNAVAILABLE_MESSAGE
from learn.auth import BrowserClosedError


class UnavailableError(Exception):
    pass


def make_backend() -> Backend:
    return Backend(
        name="fake",
        model="fake-model",
        make_client=lambda: object(),
        check_connection=lambda client: None,
        unavailable_error=UnavailableError,
    )


def make_session(run_turn_fn, executor) -> AgentSession:
    return AgentSession(
        llm_client=object(),
        backend=make_backend(),
        tools={},
        executor=executor,
        run_turn_fn=run_turn_fn,
    )


def test_ask_appends_user_message_and_returns_reply():
    calls = []

    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        calls.append(list(messages))
        messages.append({"role": "assistant", "content": "the answer"})
        usage.add(10, 5)
        return "the answer"

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        reply = asyncio.run(session.ask("what's due this week?"))

    assert reply == "the answer"
    assert session.usage.total_tokens == 15
    assert calls[0][-1] == {"role": "user", "content": "what's due this week?"}


def test_ask_runs_on_the_executors_single_worker_thread_every_time():
    thread_idents = []

    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        thread_idents.append(threading.get_ident())
        return "ok"

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        asyncio.run(session.ask("first"))
        asyncio.run(session.ask("second"))

    assert len(thread_idents) == 2
    assert thread_idents[0] == thread_idents[1]
    assert thread_idents[0] != threading.get_ident()


def test_ask_transitions_to_browser_failed_on_browser_closed_error():
    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        raise BrowserClosedError("browser closed")

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        reply = asyncio.run(session.ask("question"))

        assert reply == UNAVAILABLE_MESSAGE
        assert session.state is SessionState.BROWSER_FAILED

        # A second call must short-circuit without touching run_turn_fn again.
        reply2 = asyncio.run(session.ask("another question"))
    assert reply2 == UNAVAILABLE_MESSAGE


def test_ask_reports_unavailable_error_without_changing_state():
    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        raise UnavailableError("temporarily down")

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        reply = asyncio.run(session.ask("question"))

    assert "temporarily unavailable" in reply
    assert session.state is SessionState.RUNNING


def test_reset_clears_messages_and_usage():
    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        usage.add(100, 100)
        return "answer"

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        asyncio.run(session.ask("question"))
        assert session.usage.total_tokens == 200

        session.reset()

    assert session.usage.total_tokens == 0
    assert len(session.messages) == 1
    assert session.messages[0]["role"] == "system"


def test_status_reports_non_sensitive_fields_only():
    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(lambda *a, **k: "unused", executor)
        status = session.status()

    assert status == {
        "backend": "fake",
        "model": "fake-model",
        "state": "running",
        "message_count": 1,
        "total_tokens": 0,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/discord_adapter/test_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discord_adapter.session'`

- [ ] **Step 3: Write `discord_adapter/session.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/discord_adapter/test_session.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add discord_adapter/session.py tests/discord_adapter/test_session.py
git commit -m "feat: add AgentSession, sole owner of the Playwright worker thread"
```

---

### Task 5: `discord_adapter/bot.py`

**Files:**
- Create: `learn-agent/discord_adapter/bot.py`
- Test: `learn-agent/tests/discord_adapter/test_bot.py`, `learn-agent/tests/discord_adapter/test_import_boundaries.py`
- Modify: `learn-agent/requirements.txt` (add `discord.py` — needed now since `discord_adapter/bot.py` imports `discord`)

**Interfaces:**
- Consumes: `discord_adapter.session.AgentSession` (`.ask(text) -> str` [async], `.reset() -> None`, `.status() -> dict`); `discord_adapter.ratelimit.RateLimiter` (`.allow() -> bool`); `discord_adapter.config.DiscordConfig` (`.allowed_user_id: int`), `discord_adapter.config.MAX_INCOMING_MESSAGE_LENGTH`.
- Produces: `discord_adapter.bot.DISCORD_MESSAGE_LIMIT` (int, `2000`), `discord_adapter.bot.chunk_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]`, `discord_adapter.bot.format_status(status: dict) -> str`, `discord_adapter.bot.handle_incoming(session, rate_limiter, lock, config, author_id, is_dm, text) -> list[str]` (async), `discord_adapter.bot.LearnDiscordClient(session, config)` (a `discord.Client` subclass).

- [ ] **Step 1: Add `discord.py` to `requirements.txt` and install it**

Edit `requirements.txt`, add a line: `discord.py>=2.4.0`

Run: `pip install -r requirements.txt` (venv active)
Expected: installs successfully.

- [ ] **Step 2: Write the failing tests for the discord.py-independent core logic**

Create `tests/discord_adapter/test_bot.py`:

```python
import asyncio

import pytest

from discord_adapter.bot import (
    BUSY_MESSAGE,
    RATE_LIMITED_MESSAGE,
    RESET_CONFIRMATION,
    TOO_LONG_MESSAGE_TEMPLATE,
    chunk_message,
    format_status,
    handle_incoming,
)
from discord_adapter.config import DiscordConfig


class FakeSession:
    def __init__(self, reply: str = "the answer"):
        self.reply = reply
        self.asked: list[str] = []
        self.reset_called = False
        self._status = {
            "backend": "fake",
            "model": "fake-model",
            "state": "running",
            "message_count": 1,
            "total_tokens": 0,
        }

    async def ask(self, text: str) -> str:
        self.asked.append(text)
        return self.reply

    def reset(self) -> None:
        self.reset_called = True

    def status(self) -> dict:
        return self._status


class AlwaysAllowRateLimiter:
    def allow(self) -> bool:
        return True


class AlwaysDenyRateLimiter:
    def allow(self) -> bool:
        return False


def make_config(allowed_user_id: int = 42) -> DiscordConfig:
    return DiscordConfig(
        bot_token="fake",
        allowed_user_id=allowed_user_id,
        max_messages_per_minute=5,
        max_messages_per_hour=50,
    )


def test_ignores_non_dm_messages():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=False,
            text="what's due this week?",
        )
    )
    assert replies == []
    assert session.asked == []


def test_ignores_messages_from_unauthorized_user():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(allowed_user_id=42),
            author_id=999,
            is_dm=True,
            text="what's due this week?",
        )
    )
    assert replies == []
    assert session.asked == []


def test_rejects_message_over_length_limit():
    session = FakeSession()
    long_text = "x" * 5000
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text=long_text,
        )
    )
    assert replies == [TOO_LONG_MESSAGE_TEMPLATE.format(length=5000)]
    assert session.asked == []


def test_rate_limited_message_never_reaches_session():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysDenyRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="what's due this week?",
        )
    )
    assert replies == [RATE_LIMITED_MESSAGE]
    assert session.asked == []


def test_reset_command_handled_without_calling_session_ask():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="!reset",
        )
    )
    assert replies == [RESET_CONFIRMATION]
    assert session.reset_called is True
    assert session.asked == []


def test_status_command_handled_without_calling_session_ask():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="!status",
        )
    )
    assert replies == [format_status(session.status())]
    assert session.asked == []


def test_busy_lock_rejects_concurrent_message():
    session = FakeSession()
    lock = asyncio.Lock()

    async def scenario():
        await lock.acquire()
        try:
            return await handle_incoming(
                session=session,
                rate_limiter=AlwaysAllowRateLimiter(),
                lock=lock,
                config=make_config(),
                author_id=42,
                is_dm=True,
                text="what's due this week?",
            )
        finally:
            lock.release()

    replies = asyncio.run(scenario())
    assert replies == [BUSY_MESSAGE]
    assert session.asked == []


def test_ordinary_question_is_asked_and_chunked():
    session = FakeSession(reply="short answer")
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="what's due this week?",
        )
    )
    assert session.asked == ["what's due this week?"]
    assert replies == ["short answer"]


def test_chunk_message_splits_long_text_without_exceeding_limit():
    text = "word " * 1000  # 5000 chars
    chunks = chunk_message(text, limit=100)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_chunk_message_returns_single_chunk_when_short():
    assert chunk_message("hello") == ["hello"]


def test_format_status_never_includes_course_data():
    status = {
        "backend": "gemini",
        "model": "gemini-3.1-flash-lite",
        "state": "running",
        "message_count": 4,
        "total_tokens": 1234,
    }
    text = format_status(status)
    assert "gemini" in text
    assert "1,234" in text
    assert "running" in text
```

Create `tests/discord_adapter/test_import_boundaries.py`:

```python
import ast
from pathlib import Path

BOT_PY = Path(__file__).resolve().parent.parent.parent / "discord_adapter" / "bot.py"


def _imported_module_names() -> set[str]:
    tree = ast.parse(BOT_PY.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_bot_module_never_imports_learn_or_agent_tools():
    names = _imported_module_names()
    for name in names:
        assert not name.startswith("learn"), f"discord_adapter/bot.py must not import {name}"
        assert name not in ("agent.tools", "agent.course_registry"), (
            f"discord_adapter/bot.py must not import {name}"
        )


def test_agent_session_is_never_constructed_inside_bot_module():
    source = BOT_PY.read_text()
    assert "AgentSession(" not in source
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/discord_adapter/test_bot.py tests/discord_adapter/test_import_boundaries.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discord_adapter.bot'` (the import-boundary test itself will fail with `FileNotFoundError` reading a file that doesn't exist yet).

- [ ] **Step 4: Write `discord_adapter/bot.py`**

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/discord_adapter/test_bot.py tests/discord_adapter/test_import_boundaries.py -v`
Expected: PASS (13 tests)

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `python -m pytest`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add discord_adapter/bot.py tests/discord_adapter/test_bot.py tests/discord_adapter/test_import_boundaries.py requirements.txt
git commit -m "feat: add Discord transport layer (bot.py) with commands, rate limiting, chunking"
```

---

### Task 6: `discord_bot.py` entry point + `.env.example`

**Files:**
- Create: `learn-agent/discord_bot.py`
- Modify: `learn-agent/.env.example`

**Interfaces:**
- Consumes: `agent.backend.resolve`, `agent._model_arg`, `agent._connect`, `agent.connect_and_authenticate`, `agent.AgentStartupError` (all from Task 1); `discord_adapter.config.load_config`, `discord_adapter.config.DiscordConfigError`; `discord_adapter.session.AgentSession`; `discord_adapter.bot.LearnDiscordClient`.
- Produces: the `discord_bot.py` CLI entry point. No other module depends on this file (it's the outermost layer), so no automated test — it is exercised by the manual smoke test below, the same way `agent.py`'s `run_interactive_agent()` already is.

- [ ] **Step 1: Add Discord env vars to `.env.example`**

Add to the end of `.env.example`, following its existing section style:

```
# Required to run discord_bot.py: your bot's token (from
# https://discord.com/developers/applications) and YOUR Discord user ID
# (enable Developer Mode in Discord's settings, then right-click your own
# name and "Copy User ID"). Only DMs from this exact user ID are answered --
# everyone else is silently ignored. Get a key at the URL above.
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_ALLOWED_USER_ID=your-discord-user-id

# Optional: override the default persistent rate limit (5/minute, 50/hour)
# discord_adapter/ratelimit.py applies on top of TurnGuard's existing
# per-turn tool-call limits (agent/limits.py).
# DISCORD_MAX_MESSAGES_PER_MINUTE=5
# DISCORD_MAX_MESSAGES_PER_HOUR=50
```

- [ ] **Step 2: Write `discord_bot.py`**

```python
"""Discord DM entry point for the LEARN agent.

    python discord_bot.py                    Connect the LLM backend, open
                                              LEARN auth (same visible
                                              Chromium + WatIAM/Duo flow as
                                              agent.py), wait for you to
                                              finish logging in, then start
                                              listening for Discord DMs.
    python discord_bot.py --model qwen       Use local Ollama/Qwen instead
                                              of the default Gemini backend
                                              (same --model flag as agent.py).
    python discord_bot.py --debug            Print tool-call debug info to
                                              this terminal only -- never to
                                              Discord.

This file is a TRANSPORT, not a second implementation of LEARN auth or the
agent loop -- it reuses agent.py's _connect()/connect_and_authenticate()
unmodified and hands everything else to discord_adapter/. See
docs/superpowers/specs/2026-08-19-discord-adapter-design.md for the design.

Fails closed: if the LLM backend or LEARN authentication doesn't succeed,
this exits before the Discord client ever connects -- the bot is never
visibly online while unable to actually answer anything.
"""

import sys
from concurrent.futures import ThreadPoolExecutor

import agent as agent_module
from agent import backend as backend_module
from discord_adapter.bot import LearnDiscordClient
from discord_adapter.config import DiscordConfigError, load_config
from discord_adapter.session import AgentSession


def main() -> None:
    args = sys.argv[1:]

    try:
        discord_config = load_config()
    except DiscordConfigError as exc:
        print(f"\n{exc}")
        sys.exit(1)

    backend = backend_module.resolve(agent_module._model_arg(args))

    print("LEARN Discord Agent")
    print("────────────────────")

    llm_client = agent_module._connect(backend)

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        context, playwright, learn_client, tools = executor.submit(
            agent_module.connect_and_authenticate, backend
        ).result()
    except agent_module.AgentStartupError as exc:
        print(f"\n{exc}")
        executor.shutdown(wait=False)
        sys.exit(1)

    session = AgentSession(
        llm_client=llm_client,
        backend=backend,
        tools=tools,
        executor=executor,
        debug="--debug" in args,
    )

    print("Discord bot connecting...")
    client = LearnDiscordClient(session=session, config=discord_config)
    try:
        client.run(discord_config.bot_token)
    finally:
        try:
            context.close()
        except Exception:
            pass
        playwright.stop()
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the full test suite one more time**

Run: `python -m pytest`
Expected: all pass (no test imports `discord_bot.py` itself, so this step is a pure regression check on everything else).

- [ ] **Step 4: Manual smoke test (cannot be automated — requires a real Discord bot token and a real LEARN login)**

1. Create a Discord application + bot at https://discord.com/developers/applications, enable the "Message Content Intent" under Bot settings, invite it to your own account (or just note its bot user — DMs work without a shared server as long as you DM it first isn't required for *you* messaging *it*; confirm via Discord's own docs/UI if the bot needs to share a server with you to receive your DM — if so, create a minimal private server and add the bot there, then DM it directly).
2. Add `DISCORD_BOT_TOKEN` and `DISCORD_ALLOWED_USER_ID` (your own Discord user ID) to `.env`.
3. Run `python discord_bot.py` from `learn-agent/`.
4. Confirm: LLM connects, browser opens, you complete WatIAM/Duo, "Discord bot connecting..." prints, then "Discord bot connected as ... -- listening for DMs."
5. DM the bot: "what assignments do I have due this week?" — confirm you get a real, LEARN-derived answer.
6. DM `!status` — confirm it replies with backend/state/message-count/tokens, no course data.
7. DM `!reset` — confirm confirmation reply, then ask a follow-up that would only make sense with prior context, confirm the bot no longer has it.
8. Send two messages back-to-back quickly — confirm the second gets the "still working" busy reply while the first is in flight.
9. Ctrl-C the process — confirm it exits cleanly (browser closes).

- [ ] **Step 5: Commit**

```bash
git add discord_bot.py .env.example
git commit -m "feat: add discord_bot.py entry point"
```

---

## Self-Review Notes

- **Spec coverage:** every numbered item in the spec's "Module-by-module" and error-handling sections maps to a task above — config/rate-limit/session/bot/entry-point split matches the spec's file list exactly, including the `!reset`/`!status`-never-reach-Gemini rule (Task 5), fail-closed startup (Task 6), one-way `BROWSER_FAILED` state (Task 4), and the import-boundary/single-construction invariants (Task 5's `test_import_boundaries.py`).
- **Type consistency checked:** `AgentSession.ask()` is `async` everywhere it's used (Task 4's tests wrap it in `asyncio.run`; Task 5's `handle_incoming()` awaits it); `Backend.unavailable_error` is used identically to `agent.py`'s existing `except backend.unavailable_error as exc:` pattern; `run_turn`'s positional argument order (`ollama_client, model, messages, tools, debug, usage`) is passed identically in `AgentSession.ask()` and matched by every fake `run_turn_fn` in Task 4's tests.
- **No placeholders:** every step has runnable code, not a description of code.
