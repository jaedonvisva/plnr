# Discord DM Adapter for the LEARN Agent

Status: approved by user, pending implementation plan.

## Goal

Add Discord DMs as a second, portable frontend to the existing LEARN agent,
without modifying any of the existing agent/auth/tool/security code. One
command starts the process; it authenticates against LEARN exactly as
`agent.py` does today (visible Chromium, manual WatIAM + Duo); once
authenticated it starts listening for DMs from one authorized Discord user
and answers questions via the existing `run_turn()` path.

Eventual goal (out of scope for this milestone, but shapes the design): this
process moves from a Mac to a Windows/Linux home PC, and Discord becomes the
first of possibly several frontends (CLI, web) in front of one long-running
agent service. Nothing in this milestone should make that harder.

## Non-goals / explicit constraints

- Do not modify `LearnClient`, `learn/browser.py`, `learn/auth.py`,
  `learn/credentials.py`, `CourseRegistry`, `TurnGuard`, `agent/tools.py`,
  `agent/serialize.py`, `agent/system_prompt.py`, `agent/loop.py`'s
  `run_turn()` semantics, or the Gemini/Ollama backend adapters.
- No alternate tool-execution path. Every LEARN-derived answer goes through
  the exact same `run_turn()` → `TurnGuard` → whitelisted tool →
  `CourseRegistry`/validation → `LearnClient` chain the CLI already uses.
- No macOS-specific code in the new adapter. (The existing Playwright launch
  in `learn/browser.py` is already cross-platform and is not touched here.)
- Single authorized user, DMs only. No multi-user support, no server/channel
  support, in this milestone.
- No cross-process/cross-restart persistence of conversation history — only
  cross-*message* persistence within one running process, same lifetime
  model as the CLI's `messages` list has today (reset only via restart, or
  the new `!reset` command).

## Architecture

```
Discord DM
    |
    v
discord_adapter/bot.py        (transport only: auth, commands, rate limit,
    |                           chunking -- never imports learn.* or agent.tools)
    v
discord_adapter/session.py    (AgentSession: owns the single worker thread,
    |                           messages, usage, LearnClient, tools, state)
    v
agent/loop.py::run_turn()     (UNCHANGED)
    |
    v
TurnGuard -> whitelisted tool -> CourseRegistry -> LearnClient -> Valence
```

`discord_adapter/` is a new top-level package, a sibling of `agent/` and
`learn/`. It depends on `agent/` (via `AgentSession`, which is the only
thing that imports `agent.tools`, `agent.loop`, `agent.course_registry`
indirectly through `build_tools`) and does not depend on `learn/` directly
at all — `learn.client.LearnClient` is constructed once during startup
orchestration and handed to `AgentSession`, never touched again by the
adapter.

## New/changed files

- **`discord_adapter/config.py`** (new) — reads `DISCORD_BOT_TOKEN`,
  `DISCORD_ALLOWED_USER_ID` (int), and optional
  `DISCORD_MAX_MESSAGES_PER_MINUTE` / `DISCORD_MAX_MESSAGES_PER_HOUR`
  overrides from `.env` via `python-dotenv`, same pattern as
  `learn/credentials.py`/`agent/gemini_client.py`. Fails with a clear error
  if the token or allowed-user-id is missing — mirrors
  `agent/llm.py`/`agent/gemini_client.py`'s `check_connection` pattern of
  failing loudly at startup, not at first use.
- **`discord_adapter/session.py`** (new) — `AgentSession`:
  - Constructed with an already-authenticated `LearnClient`, the built
    `tools` dict, the connected LLM client, and the resolved `Backend`.
  - Owns a `concurrent.futures.ThreadPoolExecutor(max_workers=1)` — the
    **exclusive owner** of everything Playwright-derived from this point
    on. Nothing outside `AgentSession` ever touches `LearnClient`, the
    `tools` callables, or `run_turn()` directly.
  - State machine: `RUNNING` → `BROWSER_FAILED` (one-way; no automatic
    recovery — see Error handling below).
  - `messages: list[dict]` (starts as `[{"role": "system", "content":
    SYSTEM_PROMPT}]`) and `usage: Usage`, exactly mirroring the CLI's
    locals today, but owned by the session instead of being loop-local
    variables in `agent.py`.
  - `async def ask(self, text: str) -> str` — the only entry point the
    adapter calls for a real question. Internally: if state is
    `BROWSER_FAILED`, return a fixed unavailable message without touching
    the executor. Otherwise append the user message, call
    `run_turn(llm_client, model, self.messages, self.tools, usage=turn_usage)`
    via `loop.run_in_executor(self._executor, ...)`, add `turn_usage` into
    `self.usage`, and return the reply string. Catches
    `auth.BrowserClosedError` (transitions state to `BROWSER_FAILED`,
    returns the fixed unavailable message) and `backend.unavailable_error`
    (state unchanged, returns a "try again" message) — same two branches
    `run_interactive_agent()` already has, just returning strings instead
    of printing.
  - `def reset(self) -> None` — resets `messages` to just the system
    prompt and `usage` to a fresh `Usage()`. Synchronous, no executor
    needed (doesn't touch Playwright/LearnClient).
  - `def status(self) -> dict` — returns non-sensitive state only:
    backend name/model, session state (`running`/`browser_failed`),
    message count in the conversation, total tokens used this session.
    Never includes course data, tokens/cookies, or raw messages.
- **`discord_adapter/bot.py`** (new) — a `discord.py` `discord.Client`
  subclass taking an `AgentSession` in its constructor. `!reset`/`!status`
  are matched as exact-string prefixes inside `on_message` rather than via
  `discord.ext.commands.Bot`'s command framework — there are only two of
  them and both need direct access to `AgentSession`, so the extra
  framework layer isn't earning its keep here. `on_message`:
  1. Ignore messages from the bot itself.
  2. Ignore anything that isn't a DM (`isinstance(message.channel,
     discord.DMChannel)`) or isn't from `config.allowed_user_id` — silently,
     no reply, so a stranger gets no confirmation the bot is listening.
  3. Reject (with a reply) if `len(message.content) >
     MAX_INCOMING_MESSAGE_LENGTH` (default 4000) — never reaches
     `session.ask()`.
  4. Enforce the persistent rate limit (see below) — reply with a
     cooldown message and return if exceeded.
  5. If the message is exactly `!reset` or `!status`, handle it directly
     in Python (`session.reset()` / `session.status()`) and reply —
     **never** passed to `session.ask()`, so these can never reach
     Gemini/Ollama.
  6. Otherwise, if a turn is already in flight (an `asyncio.Lock` around
     `session.ask()`), reply "still working on your last question, one
     moment" and return — this is the "simple cooldown between messages"
     from the earlier discussion, now sitting alongside the persistent
     rate limiter rather than replacing it.
  7. Otherwise, acquire the lock, `await session.ask(message.content)`,
     release, reply — chunked into ≤2000-character messages on
     paragraph/word boundaries if needed.
  - Never imports `learn.*` or `agent.tools`/`agent.course_registry`
    directly — enforced by a test that greps/AST-checks this file's
    imports (see Testing).
  - Reconnects: `discord.py` handles websocket reconnects internally
    within the same process/`Client` instance; `AgentSession` is
    constructed exactly once, before `bot.run()` is ever called, and
    nothing in `on_ready`/reconnect handling constructs a new one. Add a
    code comment stating this invariant plus a test asserting no
    `AgentSession(...)` call exists inside `bot.py` itself (only
    `discord_bot.py`'s startup code constructs it).
- **`discord_adapter/ratelimit.py`** (new, small) — a simple sliding-window
  counter: `MAX_MESSAGES_PER_MINUTE = 5`, `MAX_MESSAGES_PER_HOUR = 50`
  (both overridable via `discord_adapter/config.py`). Tracks timestamps of
  recent accepted messages for the single authorized user; `allow() ->
  bool`. This is deliberately separate from `TurnGuard` — `TurnGuard` is
  per-turn tool-call defense-in-depth (unchanged, untouched); this is
  cross-message/cross-hour abuse defense specific to an unattended,
  always-listening transport, per the module docstring in
  `agent/limits.py` that already anticipated this being layered on later.
- **`discord_bot.py`** (new, top level, sibling to `agent.py`) — the entry
  point. Sequence:
  1. Load config (`agent.backend` CLI-style resolution, reusing whatever
     `--model` handling `agent.py` has, plus `discord_adapter.config`).
     Any missing/invalid config fails immediately with a clear message —
     **before** anything else starts.
  2. Connect + verify the LLM backend (reuses `_connect()`'s logic).
  3. Launch the browser and wait for LEARN authentication (reuses the
     exact block `run_interactive_agent()` uses today — see "Mechanical
     extraction" below). **If authentication fails or times out, exit
     without ever starting the Discord client** — fail closed, per the
     explicit requirement that the bot must never sit there answering
     with auth errors.
  4. Construct `LearnClient`, `build_tools(client)`.
  5. Construct `AgentSession(...)`.
  6. Construct `discord_adapter.bot`'s client with that session and
     `bot.run(config.discord_token)` — this call blocks the main thread
     running `discord.py`'s asyncio loop for the life of the process.
  - Startup prints the same kind of explicit checklist the CLI already
    prints (LLM connect ✓, browser opening, LEARN auth ✓) plus a final
    "Discord bot connecting..." / "listening" line, so the sequence is
    visible when running this unattended in a terminal.
- **Mechanical extraction in `agent.py`** — the "connect LLM → launch
  browser → complete auth → build `LearnClient`/`tools`" block currently
  inlined in `run_interactive_agent()` (lines ~95-124) is pulled out into a
  small helper, e.g. `agent.py::connect_and_authenticate(backend) ->
  (context, playwright, learn_client, tools)`, used by both
  `run_interactive_agent()` (CLI, unchanged behavior) and `discord_bot.py`.
  Pure extraction — no logic changes, so this isn't "rewriting the agent",
  it's making sure the security-relevant wiring exists in exactly one
  place instead of being copy-pasted into a second entry point.

## Threading model

`run_turn()`, `LearnClient` (via `context.request`), and Playwright's sync
API are blocking and thread-affine: the sync API must be driven from the
one thread that created it. `discord.py` needs its asyncio event loop free
to service heartbeats. Resolution:

- The `ThreadPoolExecutor(max_workers=1)` inside `AgentSession` is created
  once, and its single worker thread is the one used for **every**
  `run_turn()` call for the life of the process (guaranteed by
  `max_workers=1` always reusing the same thread for sequential
  submissions).
- The **browser launch and auth wait also happen in this worker thread**
  in the final design (submitted to the executor from `discord_bot.py`'s
  startup code and awaited synchronously before proceeding) — not the
  main thread — so that literally everything Playwright-derived,
  including the initial launch, lives on one thread for the rest of the
  process's life. `discord_bot.py`'s main thread only ever does: build
  config, submit startup work to the executor, block on the result, then
  hand off to `bot.run()`.
- Invariant, enforced by comment + test: the asyncio side (`bot.py`) never
  directly references `context`, `page`, `LearnClient`, any Playwright
  object, or a tool callable — only `AgentSession.ask/reset/status`.

## Error handling

| Condition | Behavior |
|---|---|
| `auth.BrowserClosedError` during a turn | `AgentSession` transitions to `BROWSER_FAILED`. Reply: "LEARN session unavailable — restart the agent." All subsequent `ask()` calls short-circuit to the same message without touching the executor. No auto-recovery, no second Playwright context — restart is manual. |
| `backend.unavailable_error` (transient LLM 503 etc.) | State unchanged. Reply: "{backend} is temporarily unavailable, try asking again." Lock released, ready for the next message. |
| Any other unexpected exception inside `ask()` | Caught at the `AgentSession` boundary, logged to stdout, generic reply sent, process stays alive — never let one bad turn kill the bot (same philosophy as `_execute_tool()`'s catch-all in `agent/loop.py`, applied one layer up). |
| Auth fails/times out at startup | Process exits before `bot.run()` is ever called — the bot is never visibly online with a broken backend. |
| Incoming message too long | Rejected in `bot.py` before `session.ask()`, with a reply stating the limit. |
| Rate limit exceeded | Rejected in `bot.py` before `session.ask()`, with a reply stating the cooldown. |

## Commands

`!reset` and `!status` are the only two, both handled deterministically in
`bot.py` and never routed through `run_turn()`/Gemini/Ollama. `!status`
returns backend name/model, session state, conversation message count, and
session-total token usage — never course data, tokens, or raw message
content.

## Testing

Following the existing "all offline" pattern:

- `tests/discord_adapter/fake_discord.py` — a minimal fake
  message/channel/user, enough to drive `bot.py`'s `on_message` without a
  real Discord connection or bot token.
- Authorization: non-allowed user ID ignored; non-DM channel ignored;
  allowed user DM handled.
- `!reset`/`!status`: handled without invoking `session.ask()` (assert via
  a fake/spy `AgentSession`).
- Incoming-length cap and rate limiter: both reject before `session.ask()`
  is called.
- Chunking: a reply over 2000 chars is split into multiple sends, none
  exceeding the limit.
- `AgentSession.ask()`: `BrowserClosedError` → state transitions to
  `BROWSER_FAILED` and a second `ask()` call short-circuits without
  touching the (fake) executor/backend again; `backend.unavailable_error`
  → state stays `RUNNING`.
- Import-boundary test: `discord_adapter/bot.py` does not import
  `learn.*`, `agent.tools`, or `agent.course_registry` (AST-based check
  over the module source, not a runtime assertion).
- `AgentSession` construction: assert it happens exactly once in
  `discord_bot.py`'s startup path and not inside `bot.py`.

## Config / new dependency

- `requirements.txt`: add `discord.py>=2.4.0` (or current stable).
- `.env.example`: add `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USER_ID`, and
  commented-out optional `DISCORD_MAX_MESSAGES_PER_MINUTE`/`_PER_HOUR`
  overrides, following the existing file's style and security notes.

## Explicitly out of scope for this milestone

- Multi-user support, server/channel support (DM-only, single user).
- Persisting conversation history across process restarts.
- Auto-restart/process supervision (systemd/launchd) — noted as a future
  option, not built here.
- Any change to `learn/browser.py` for cross-platform behavior — it's
  already cross-platform via Playwright; this milestone doesn't need to
  touch it.
