# LEARN Agent

A read-only AI assistant for a University of Waterloo student's LEARN
(Brightspace/D2L) account. You authenticate yourself (WatIAM + Duo, in a real
visible browser — this app never sees your credentials by default); an LLM
then answers questions about your courses, assignments, grades, quizzes, and
announcements by calling a small set of read-only tools backed by
Brightspace's documented REST API. A Discord DM adapter (`discord_bot.py`)
lets you ask it questions from your phone instead of a terminal.

**The LLM backend is selectable (`--model`) and, by default, is NOT local.**
The default backend is Gemini (Google's cloud API) — your prompts and LEARN
data (course names, grades, assignment/announcement text returned by tool
calls) are sent to Google's servers by default. Pass `--model qwen` to use
local Ollama/Qwen instead, which sends nothing off-device. See
`agent/backend.py` for exactly how a `--model` value picks a backend, and
`agent/llm.py` vs `agent/gemini_client.py` for what each one actually does.

No write capability exists anywhere in this codebase, regardless of which
LLM backend is selected — every call to LEARN is a `GET`.

For engineering history, design rationale, known bugs and their fixes, and
what's left to build, see [HANDOFF.md](HANDOFF.md).

---

## Table of contents

1. [Quick start](#quick-start)
2. [Architecture](#architecture)
3. [Project structure](#project-structure)
4. [Security model](#security-model)
5. [Testing](#testing)

---

## Quick start

```bash
git clone <this repo>   # or just cd into it if you already have it
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# For local Qwen (--model qwen): Ollama must be running with qwen3:8b pulled:
#   ollama pull qwen3:8b
# For the default backend (Gemini): set GEMINI_API_KEY in .env (see below).
```

Entry points:

| Command | What it does |
|---|---|
| `python agent.py logout` | Clears the persisted LEARN session. You'll need to log in again next run. |
| `python agent.py --test-llm` | LLM connectivity check only — no LEARN/Playwright involved. |
| `python agent.py` (add `--debug` to see tool calls) | The full interactive agent: auth, then a chat loop where the LLM answers your questions by calling LEARN tools. |
| `python discord_bot.py` (add `--debug` to see tool calls in your terminal) | Same agent, reachable by DM'ing your Discord bot instead of using the terminal. |

**`--model` picks the LLM backend** (default: `gemini`, cloud — see the
warning above). Recognized values: `qwen` / `ollama` (local, `qwen3:8b` by
default), `gemini` (cloud, `gemini-3.1-flash-lite` by default — chosen for
free-tier headroom: 500 requests/day vs. 20 for `gemini-3.6-flash`, see
`agent/gemini_client.py`), or a literal
model id for either (e.g. `--model llama3:8b`, `--model gemini-2.0-flash`).
Combine with any entry point above, e.g. `python agent.py --model qwen`.

A `.env` file is required only for the default Gemini backend
(`GEMINI_API_KEY`), the Discord adapter (`DISCORD_BOT_TOKEN`,
`DISCORD_ALLOWED_USER_ID`), or optional WatIAM auto-fill. `--model qwen`
needs neither of the first two. See `.env.example` for the full list.

---

## Architecture

```
┌─────────────────────────┐
│         User             │  runs the agent.py CLI (or DMs the Discord bot)
└───────────┬──────────────┘  and asks a question
            │
            ▼
┌─────────────────────────┐
│   Waterloo WatIAM + Duo   │  the user completes this themselves, in a real
│  (visible Chromium window)│  visible browser window. This code never fills
└───────────┬──────────────┘  or reads the login form (unless .env opt-in
            │                  auto-fill is enabled — see below).
            ▼
┌─────────────────────────┐
│  Playwright BrowserContext│  learn/browser.py: launch_persistent_context()
│  (persisted to .auth/)    │  learn/auth.py: waits for authentication,
└───────────┬──────────────┘  detects it via a LIVE window.location.href
            │                  read, not Playwright's cached page.url
            ▼
┌─────────────────────────┐
│  context.request           │  Playwright's APIRequestContext, sharing the
│  (Playwright's HTTP client)│  SAME cookie jar as the browser above. This
└───────────┬──────────────┘  IS the entire Valence authentication
            │                  mechanism — plain cookies, no OAuth, no
            │                  API key. See client.py docstring.
            ▼
┌─────────────────────────┐
│      LearnClient           │  learn/client.py: 7 read-only methods,
│  (learn/client.py)          │  GET-only, typed errors (401→auth error,
└───────────┬──────────────┘  403→empty list), normalizes raw JSON into...
            │
            ▼
┌─────────────────────────┐
│  Valence REST API           │  Brightspace's documented API
│  (learn.uwaterloo.ca/d2l/  │  (/d2l/api/lp/..., /d2l/api/le/...)
│   api/...)                  │
└───────────┬──────────────┘
            │
            ▼
┌─────────────────────────┐
│  Domain models               │  learn/models.py: Course, Assignment,
│  (learn/models.py)            │  Grade, Quiz, Announcement, ContentItem
└───────────┬──────────────┘  — frozen dataclasses, typed, no D2L jargon
            │
            ▼
┌─────────────────────────┐
│  agent/aggregation.py        │  cross-course date filtering, text search,
│  (deterministic Python)      │  sorting -- Python does this, not the LLM
└───────────┬──────────────┘
            │
            ▼
┌─────────────────────────┐
│  agent/tools.py (LLM-facing)│  serializes to plain dict (agent/serialize.py),
│                              │  strips `.raw`, truncates rich-text fields
└───────────┬──────────────┘
            │
            ▼
┌─────────────────────────┐
│  agent/loop.py               │  tool-calling loop (up to 20 rounds),
│                              │  typed error handling -- backend-agnostic
└───────────┬──────────────┘
            │
            ▼
┌─────────────────────────┐
│  agent/backend.py            │  resolves --model to a backend (see
│  (picks ONE, at launch)      │  agent/llm.py vs agent/gemini_client.py)
└───────────┬──────────────┘
            │
      ┌─────┴─────┐
      ▼           ▼
┌───────────┐ ┌───────────────┐
│ Qwen 3 8B │ │ Gemini         │
│ (Ollama,  │ │ (Google's      │
│ local,    │ │ cloud API --   │
│ default   │ │ DEFAULT unless │
│ off)      │ │ --model qwen)  │
└─────┬─────┘ └───────┬───────┘
      └───────┬───────┘
              ▼
      natural-language answer → User (terminal or Discord DM)
```

**The one-sentence version:** a real browser you log into yourself produces
session cookies; Playwright's HTTP client reuses those same cookies to call
Brightspace's documented REST API; the responses get normalized into typed
Python objects; a thin tool layer exposes them as LLM-callable functions
with HTML stripped and long text truncated; the selected LLM decides which
to call and answers. Nothing here is a registered Valence "application" (no
OAuth, no ID-Key) — it's the same cookie-based access LEARN's own frontend
JavaScript uses.

---

## Project structure

```
.
├── agent.py                  # CLI entry point: --model, interactive agent, `--test-llm`, `logout` subcommand
├── discord_bot.py             # Discord DM entry point (same agent, reached via Discord instead of a terminal)
├── conftest.py                # makes `learn`/`agent` importable for pytest regardless of invocation dir
├── requirements.txt            # playwright, pytest, python-dotenv, ollama, google-genai, discord.py
├── .env.example                 # template for optional WATIAM_*/OLLAMA_*/GEMINI_*/DISCORD_* config
├── .gitignore                    # .auth/, .env, __pycache__/, .venv/, .pytest_cache/
│
├── learn/                          # the "data layer" — auth + normalized LEARN access
│   ├── browser.py                    # Playwright persistent-context lifecycle
│   ├── auth.py                        # login-completion detection
│   ├── credentials.py                  # OPT-IN WatIAM auto-fill (the one exception to "never touches credentials")
│   ├── client.py                        # LearnClient: 8 methods, Valence HTTP calls, error typing
│   ├── models.py                         # Course/Assignment/Grade/Quiz/Announcement/ContentItem dataclasses
│   └── exceptions.py                      # LearnApiError / LearnAuthenticationError / LearnToolUnavailableError
│
├── agent/                           # the LLM-facing layer — built on top of learn/, never touches Playwright
│   ├── llm.py                         # Ollama client config (env vars), connectivity check -- local only
│   ├── gemini_client.py                # Gemini backend adapter -- the ONE cloud-API module, strictly opt-in
│   ├── backend.py                       # resolves --model to a backend (see agent.py's --model flag)
│   ├── serialize.py                      # shared HTML-strip/truncate/dataclass→dict, used by tools.py + aggregation.py
│   ├── aggregation.py                     # deterministic cross-course date filtering, text search, sorting
│   ├── tools.py                            # the LLM-callable tools (explicit whitelist), wrapping LearnClient
│   ├── loop.py                              # the tool-calling loop (see MAX_TOOL_ROUNDS) -- backend-agnostic
│   └── system_prompt.py                      # the agent's instructions
│
├── discord_adapter/                 # Discord transport — auth/DM gating, rate limiting, chunking; no LEARN logic
│   ├── bot.py                         # message handling, markdown-table→code-block conversion for Discord rendering
│   ├── config.py                       # env-var config loading
│   ├── ratelimit.py                     # per-user rate limiting
│   └── session.py                        # owns the LearnClient/agent loop on a dedicated worker thread
│
└── tests/                           # all offline — no live LEARN login, no live Ollama server, no Gemini API key needed
    ├── fake_valence.py                    # fakes Playwright's context.request for LearnClient tests
    ├── fake_ollama.py                      # fakes ollama.Client for agent-loop tests
    ├── fake_gemini.py                       # fakes google.genai.Client for GeminiClient tests
    ├── fixtures/*.json                       # real-shaped (but synthetic) Valence API response fixtures
    ├── discord_adapter/                       # Discord adapter tests
    └── test_*.py                               # LearnClient, aggregation, agent loop, backend resolution, etc.
```

Two directories are created at runtime and are gitignored:
- `.auth/browser-profile/` — the persisted Chromium profile (session cookies). Delete via `python agent.py logout`.
- `.env` — if you create one (see `.env.example`).

---

## Security model

- **Credentials, by default:** never seen, stored, or transmitted anywhere
  in this codebase. The user completes WatIAM + Duo themselves, in a real
  visible browser window this code only navigates and observes (URL, DOM
  presence of a password field) — it never reads or fills the login form.
- **The one opt-in exception:** `learn/credentials.py`, only if you
  create a `.env` file yourself. Even then, Duo is never automated, and no
  other module imports or touches credentials.
- **Session storage:** `.auth/browser-profile/` is a real Chromium profile
  containing live session cookies — a bearer credential for LEARN until
  Waterloo's SSO invalidates it. Gitignored; `python agent.py logout` deletes
  it. No application-level encryption beyond whatever the OS/Chromium
  provide.
- **What the LLM can see:** only your chat messages, the tool schemas
  (name/description/parameters, auto-derived from Python function
  signatures), and plain-dict tool results with `raw` stripped, datetimes
  as ISO strings, and rich text truncated. It never imports or sees
  `learn.browser`, `learn.auth`, `BrowserContext`, Playwright, cookies, or
  `.env` — those exist one layer above it in `agent.py`'s orchestration
  code. Verified by test and by construction (`agent/serialize.py::serialize`
  unconditionally drops `raw`).
- **What's sent externally:** `learn.uwaterloo.ca` (via `LearnClient`)
  always, plus exactly one of two LLM backends depending on `--model` (see
  `agent/backend.py`):
  - `--model qwen` (or `ollama`): `localhost:11434` (Ollama) only —
    entirely local, nothing leaves your machine.
  - `--model gemini` (the **default** — no flag needed to trigger this):
    Google's Gemini API. Your chat messages and tool results — including
    real LEARN data returned by a tool call (course names, grades,
    assignment/announcement text) — are sent there.
- **Discord adapter:** only replies to DMs from the single Discord user ID
  configured in `.env` (`DISCORD_ALLOWED_USER_ID`); every other sender gets
  silent non-response. See `discord_adapter/bot.py`.
- **Write capability:** zero. Every HTTP call in `learn/client.py` is a
  `GET`. Every tool in `agent/tools.py` maps to a read-only `LearnClient`
  method. No `.click()`/`.fill()`/`.type()`/form-submission code exists
  anywhere except `learn/credentials.py`'s narrowly-scoped WatIAM
  login-form fill (opt-in, and it fills a *login* form, not anything
  inside LEARN itself).

---

## Testing

```bash
python -m pytest
```

All tests are offline — no live LEARN login, no live Ollama server, no
Gemini API key, no network access at all. See
[HANDOFF.md's Testing, in depth](HANDOFF.md#testing-in-depth) for what's
covered, what isn't, and why.
