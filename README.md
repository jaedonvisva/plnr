# LEARN Agent

Ask your D2L/Brightspace account a question, get a real answer — from a
terminal or a Discord DM.

You log into WatIAM + Duo yourself, in a real, visible browser. This app
never sees your credentials. From there, an LLM answers questions about
your courses, assignments, grades, quizzes, and announcements by calling a
small set of **read-only** tools against Brightspace's documented API.
Nothing here can click, submit, or change anything — every call is a `GET`.

By default, questions and LEARN data go to Google's Gemini API
(`--model qwen` keeps everything local via Ollama instead — see
[Quick start](#quick-start)).

Curious how it actually works, what broke during live testing, or what's
left to build? → [HANDOFF.md](HANDOFF.md)

---

## Architecture

Click any box to jump to the file that implements it.

```mermaid
flowchart TD
    U([You]) -->|terminal| A[agent.py]
    U -->|Discord DM| DB[discord_bot.py]
    DB --> DA["discord_adapter/bot.py"]
    DA --> DS["discord_adapter/session.py"]

    A --> AUTH["WatIAM + Duo<br/>(visible browser, self-login)"]
    DS --> AUTH
    AUTH --> BR["learn/browser.py<br/>persistent context → .auth/"]
    BR --> LC["learn/client.py<br/>LearnClient (GET-only)"]
    LC --> VAPI[("Valence REST API<br/>learn.uwaterloo.ca")]
    VAPI --> MODELS["learn/models.py<br/>typed domain models"]
    MODELS --> AGG["agent/aggregation.py<br/>cross-course filter/search"]
    AGG --> TOOLS["agent/tools.py<br/>LLM-callable tool whitelist"]
    TOOLS --> LOOP["agent/loop.py<br/>tool-calling loop"]
    LOOP --> BACKEND["agent/backend.py<br/>picks a backend from --model"]
    BACKEND --> QWEN["agent/llm.py<br/>Qwen · local · opt-in"]
    BACKEND --> GEMINI["agent/gemini_client.py<br/>Gemini · cloud · default"]
    QWEN --> ANS([Answer])
    GEMINI --> ANS
    ANS --> U

    click A "agent.py" "CLI entry point"
    click DB "discord_bot.py" "Discord DM entry point"
    click DA "discord_adapter/bot.py" "Message handling, auth/DM gating, chunking"
    click DS "discord_adapter/session.py" "Owns the agent loop per Discord session"
    click AUTH "learn/auth.py" "Login-completion detection"
    click BR "learn/browser.py" "Playwright persistent-context lifecycle"
    click LC "learn/client.py" "LearnClient: Valence HTTP calls, typed errors"
    click MODELS "learn/models.py" "Course/Assignment/Grade/Quiz/Announcement dataclasses"
    click AGG "agent/aggregation.py" "Deterministic date filtering, search, sorting"
    click TOOLS "agent/tools.py" "The only thing the LLM can call"
    click LOOP "agent/loop.py" "Tool-calling loop, up to 20 rounds"
    click BACKEND "agent/backend.py" "Resolves --model to a concrete backend"
    click QWEN "agent/llm.py" "Local Ollama client config"
    click GEMINI "agent/gemini_client.py" "The one cloud-API module, strictly opt-in"
```

Full hop-by-hop breakdown (with the "why" behind each box) in
[HANDOFF.md's architecture diagram](HANDOFF.md#full-architecture-diagram).

---

## Quick start

```bash
git clone <this repo>
cd <this repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

```bash
python agent.py                 # chat in your terminal
python discord_bot.py           # chat over Discord DM instead
```

| Flag / command | Does what |
|---|---|
| `--model qwen` | Local Ollama (`qwen3:8b`), nothing leaves your machine |
| `--model gemini` (default) | Google's Gemini API — needs `GEMINI_API_KEY` in `.env` |
| `--debug` | Print tool calls to your terminal, never to Discord |
| `python agent.py logout` | Wipe the saved LEARN session |
| `python agent.py --test-llm` | Check LLM connectivity only, no browser |

`.env.example` has the full list of optional config (`GEMINI_API_KEY`,
`DISCORD_BOT_TOKEN` + `DISCORD_ALLOWED_USER_ID`, optional WatIAM auto-fill).

---

## Layout

```
agent.py             CLI entry point
discord_bot.py        Discord DM entry point
learn/                 auth + Brightspace API client, normalized into typed models
agent/                  LLM tool layer + the tool-calling loop (Qwen or Gemini)
discord_adapter/         Discord transport — DM gating, rate limiting, chunking
tests/                    177 tests, fully offline
```

---

## Security, in short

- **No credentials, by default.** You complete WatIAM + Duo yourself; this
  code only watches the URL and DOM, never fills the login form.
- **Session, not password, on disk.** `.auth/browser-profile/` holds live
  cookies, gitignored, wiped with `python agent.py logout`.
- **The LLM sees your data, never your session.** Only chat + tool results
  (HTML-stripped, `raw` fields dropped) reach the model — never cookies,
  never Playwright, never `.env`.
- **Gemini is the default backend** — your prompts and LEARN data go to
  Google unless you pass `--model qwen`, which keeps everything local.
- **Discord bot replies to exactly one user ID.** Everyone else gets
  silence, not an error.
- **Zero write capability**, anywhere, for either backend.

Design rationale behind these choices is in [HANDOFF.md](HANDOFF.md).

---

## Testing

```bash
python -m pytest
```

Fully offline — no live LEARN login, no LLM server, no network calls.
Details in [HANDOFF.md](HANDOFF.md#testing-in-depth).
