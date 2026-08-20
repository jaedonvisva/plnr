# LEARN Agent — Handoff / Engineering Notes

This document is written so that a new person or a fresh coding agent can
pick this project up from scratch: what exists, why every non-obvious
decision was made, what's been tested and how, what's known to be broken or
missing, and where to go next. See [README.md](README.md) for the
project-level description (what it does, quick start, architecture,
security model).

---

## Table of contents

1. [Full architecture diagram](#full-architecture-diagram)
2. [Module-by-module reference](#module-by-module-reference)
3. [Key engineering decisions, and why](#key-engineering-decisions-and-why)
4. [Bugs found during live testing (and their fixes)](#bugs-found-during-live-testing-and-their-fixes)
5. [Testing, in depth](#testing-in-depth)
6. [Known limitations / open items](#known-limitations--open-items)
7. [How to continue this project](#how-to-continue-this-project)

---

## Full architecture diagram

README.md has the short version. This is the hop-by-hop breakdown.

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

---

## Module-by-module reference

### `learn/browser.py`
`launch_browser()` calls Playwright's `launch_persistent_context()` — not
`launch()` + `new_context()`. This is the single most important choice in
the whole auth layer: a persistent context writes its entire Chrome profile
(cookies, local storage) to `.auth/browser-profile/` on disk, which is what
lets you skip WatIAM/Duo on subsequent runs. `clear_session()` just deletes
that directory (used by `agent.py logout`).

Returns `(context, playwright)` — callers must call `playwright.stop()`
themselves; closing the context alone does not stop the Node.js driver
process underneath (see the `EPIPE` note in the bugs section below).

### `learn/auth.py`
Two jobs: detect when the user has finished logging in, and never touch the
login form itself.

- `current_url(page)` — reads `window.location.href` via `page.evaluate()`,
  **not** `page.url`. This exists because of a real bug found in live
  testing (see below): `page.url` is a Playwright-cached value that can go
  permanently stale mid-session during a multi-hop SSO redirect chain.
- `is_authenticated(page, url)` — three checks: host is `learn.uwaterloo.ca`,
  path starts with `/d2l/`, and (defensively) no `<input type="password">`
  is present. No network calls beyond what's already needed to read the URL
  and query the DOM.
- `wait_for_authentication(page, timeout=900s, poll=2s)` — polls the above
  in a loop. Registers a `context.on("close", ...)` listener to detect the
  *entire browser process* being killed (not just a tab) — `page.is_closed()`
  alone doesn't catch that case; this was also a live-found bug (see below).
  Raises `BrowserClosedError` if so.

### `learn/credentials.py`
The **one** module in this project that ever touches a credential, and only
because the user explicitly requested it (see the "iMessage agent" design
discussion — the goal is eventually running this unattended). Everything
else in this project still has the original "never touches credentials"
guarantee by default.

- Reads `WATIAM_USERNAME`/`WATIAM_PASSWORD` from a local `.env` file (via
  `python-dotenv`) — nothing is hardcoded, nothing is passed on the command
  line.
- `maybe_autofill_login(page)` fills WatIAM's real ADFS login form fields
  (`#userNameInput`, `#passwordInput`, `#submitButton` — confirmed live
  against the actual form at adfs.uwaterloo.ca) and submits. The live form
  is two-step: a username-only screen, then a password screen revealed
  after clicking `#nextButton`. **`#nextButton` and `#submitButton` are two
  separate elements, not the same one reused** — `#submitButton` is
  `display:none` and non-interactable until after `#nextButton` is
  clicked. (An earlier version of this function assumed they were the same
  element and clicked `#submitButton` on the username step, which silently
  did nothing — confirmed live, then fixed.) **Duo is never touched** —
  whatever happens after submitting (straight through if Duo's
  browser-trust cookie is still valid, or a Duo push that needs you) is
  handled identically by the same `wait_for_authentication()` as always.
- Returns `False` and does nothing if no `.env`/credentials are present —
  every script's default behavior is completely unaffected.
- **Explicit security trade-off, not hidden:** a session cookie in `.auth/`
  is time-and-scope-limited if leaked. A plaintext WatIAM password in `.env`
  is not — it's a fully reusable credential. This is real risk kept on disk
  intentionally, at the user's request, for a specific future goal.

### `learn/client.py`
`LearnClient` — the only thing that talks to Valence.

- Constructor takes a Playwright `BrowserContext` and stores `context.request`
  (its `APIRequestContext`). This is the entire authentication mechanism:
  `context.request` automatically shares the same cookie jar as the browser
  window the user logged into. No `Authorization` header, no OAuth token, no
  ID-Key HMAC signature — anywhere. Verified empirically, not assumed (see
  the full architecture audit that was done mid-project — the "Executive
  Summary" of that audit is effectively encoded in this module's docstring).
- `_versions()` calls `GET /d2l/api/versions/` once and caches the `le`/`lp`
  version strings, rather than hardcoding a version number that could go
  stale after a Brightspace upgrade.
- `_get(url)` is the single low-level HTTP method every public method routes
  through. It classifies every response:
  - `401` → raises `LearnAuthenticationError` (session expired — **never**
    silently becomes an empty list)
  - `403` → raises `LearnToolUnavailableError` (tool not enabled for this
    specific course, or a permission error Valence doesn't let us
    distinguish further — see [Known limitations](#known-limitations--open-items))
  - any other non-2xx → raises `LearnApiError`
  - defensive fallback: even a `200` with an `{"Errors": [...]}` body is
    treated the same as the corresponding status would be
- `_get_or_empty(url)` catches only `LearnToolUnavailableError` and returns
  `None` — used by every `get_*` method so "this course doesn't have Grades
  enabled" cleanly becomes `[]`, while an expired session still propagates
  as an exception up to the caller.
- Seven public methods, each hitting one documented Valence endpoint (see
  table below), each returning a list of typed models from `learn/models.py`
  — **except** `get_discussion_forums()`, which returns raw dicts because no
  domain model was ever requested for it (out of scope, deliberately).
- `get_assignments()` additionally calls `get_grades()` internally and joins
  grade data onto each assignment by matching `GradeItemId` — Dropbox
  folders don't carry the student's grade themselves.

**Discovered Valence endpoints** (all official/documented, all read-only,
all verified live against a real account):

| Data | Endpoint |
|---|---|
| API versions (queried once, cached) | `GET /d2l/api/versions/` |
| Courses (enrollments) | `GET /d2l/api/lp/{lp}/enrollments/myenrollments/` |
| Assignments (Dropbox) | `GET /d2l/api/le/{le}/{orgUnitId}/dropbox/folders/` |
| Grade item definitions | `GET /d2l/api/le/{le}/{orgUnitId}/grades/` |
| Your grade values | `GET /d2l/api/le/{le}/{orgUnitId}/grades/values/myGradeValues/` |
| Quizzes | `GET /d2l/api/le/{le}/{orgUnitId}/quizzes/` |
| Announcements/news | `GET /d2l/api/le/{le}/{orgUnitId}/news/` |
| Discussion forums | `GET /d2l/api/le/{le}/{orgUnitId}/discussions/forums/` |
| Course content tree | `GET /d2l/api/le/{le}/{orgUnitId}/content/toc` |

An internal (undocumented) endpoint, `/d2l/le/manageCourses/api/mycourses`
(used by LEARN's own "My Courses" homepage widget), was also observed during
investigation but is **not used** — the official `enrollments/myenrollments/`
endpoint returns a superset of the same data. Ranking, as actually observed:
1. Official Valence API — used exclusively, covers everything needed.
2. Internal web endpoints — seen, not needed, would only be a fallback.
3. HTML/DOM scraping — never needed at all, for anything.

### `learn/models.py`
Six frozen dataclasses (`Course`, `Assignment`, `Grade`, `Quiz`,
`Announcement`, `ContentItem`). Every field exists because it was **observed
in a real live response** — nothing is guessed from D2L's general API docs.
Each carries a `raw: dict | None` field (excluded from `repr`/equality via
`field(repr=False, compare=False)`) holding the original JSON, so nothing
useful is thrown away even though it's not part of the typed surface.

`parse_iso8601()` is the one shared normalization helper: D2L timestamps are
already UTC (`...Z` suffix); this parses them into timezone-aware
`datetime` objects in UTC — never naive, never silently converted to local
time.

Fields deliberately **excluded** because they were checked and found
unreliable, not because no one thought of them:
- `Assignment.submission_status` — `TotalUsersWithSubmissions` etc. were
  observed as `-1` (unpopulated) for the Student role in every response.
- `Assignment.url` — no real per-assignment browsable link field exists;
  `ActivityId` is an opaque LTI-style identifier, not a page URL.
- `Quiz.due_at` — the live quiz response captured during investigation was
  truncated before a due-date field could be confirmed to exist. Not added
  rather than guessed.
- Course-level "current grade" — no reliable aggregate endpoint exists;
  `/grades/final/values/myGradeValue` 404'd during investigation. Not
  computed by summing weighted items either, since that's the exact kind of
  "misleading calculated value" this project has repeatedly guarded against
  (see the grade-handling rules in the system prompt).

### `learn/exceptions.py`
`LearnApiError` (base, carries `status`/`url`) → `LearnAuthenticationError`
(401) and `LearnToolUnavailableError` (403). Exists specifically to fix a
real design flaw found during an architecture audit: without typed
exceptions, every non-2xx response looked identical to "this course
legitimately has zero items" — including an expired session, which would
make a caller (and eventually the agent) believe the user simply has no
assignments.

### `agent/llm.py`
All Ollama configuration lives here, driven by env vars
(`OLLAMA_MODEL`, default `qwen3:8b`; `OLLAMA_BASE_URL`, default
`http://localhost:11434`) — never hardcoded elsewhere in the codebase.
`check_connection()` calls `client.list()` and raises `OllamaUnavailableError`
with an actionable message if the server is unreachable or the configured
model isn't pulled, so failures surface at the right layer (`agent.py`
checks this before even opening the browser). Local only — nothing here
sends anything off-device.

### `agent/gemini_client.py`
The one cloud-API module in the project, strictly opt-in via `--model
gemini` (the current default backend — see `agent/backend.py`). Reads
`GEMINI_API_KEY`/`GEMINI_MODEL` from `.env` the same way `learn/credentials.py`
reads WatIAM's. `GeminiClient.chat()` is written to satisfy the exact same
duck-typed interface `agent/loop.py` already expected from `ollama.Client`
(see `tests/fake_ollama.py`'s shape) — `response.message.content` /
`response.message.tool_calls[i].function.name`/`.arguments`. Because
`agent/loop.py` was already written against a duck-typed client rather than
literally `ollama.Client`, it needed **zero changes** to support this second
backend. Tool schemas are auto-derived from the plain Python callables in
`agent/tools.py` by the `google-genai` SDK itself (same idea as Ollama's own
auto-schema derivation); automatic tool *execution* is explicitly disabled
so `agent/loop.py`'s `_execute_tool()` still does the actual calling and
error handling, identically to the Ollama path.

**What actually leaves your machine when this backend is selected:** the
system prompt, your messages, tool-call arguments, and tool *results* —
which include your real LEARN data (course names, grades, assignment and
announcement text) once a tool call returns something — are sent to
Google's Gemini API. Nothing about this file hides that.

### `agent/backend.py`
The single place a `--model` CLI value becomes a concrete backend. See its
module docstring for the exact resolution rules (`qwen`/`ollama` aliases,
`gemini` alias, or a literal model id routed by whether it starts with
`"gemini"`). `DEFAULT_MODEL` is Gemini's default model — omitting `--model`
entirely uses Gemini, not local Qwen.

### `agent/tools.py`
The **only** thing the LLM can call — the boundary between the normalized
data layer and Qwen. `build_tools(client)` returns a `{name: callable}`
dict bound to one authenticated `LearnClient`.

- `_serialize(value, truncate=True)` recursively converts a dataclass (or
  list of them) into a plain dict: excludes `raw`, converts `datetime` →
  ISO string, and — for any field named `body`, `description`, or
  `comments` — strips HTML and truncates to 500 characters with a
  `"...[truncated -- use get_announcement_detail for the full text]"`
  marker. This exists because of a real, live-reproduced hang: one course
  had 52 announcements, each with a full HTML body; asking about "all my
  final exams" made the agent call `get_announcements()` across 4 courses
  (110 items total), which was enough text to make a single local-model
  turn extremely slow.
- Truncation lives **here**, not in `learn/`. The tests and any future
  non-LLM consumer still get full untruncated data from
  `LearnClient` directly — this is specifically a "make this safe for an
  8B-parameter model's context window" concern, not a property of the data
  itself.
- Item **counts** are deliberately never capped (per explicit instruction)
  — only the length of each item's rich-text field.
- `get_announcement_detail(course_id, announcement_id)` is the escape hatch:
  fetches the full, untruncated (but still HTML-stripped) text of one
  specific announcement, added specifically so truncation doesn't lose real
  information permanently.
- `get_courses()`'s docstring explicitly says "takes NO arguments" — added
  after a live-reproduced bug where the model invented a nonexistent
  `filter` kwarg when the user's query already named a specific course.

### `agent/loop.py`
The tool-calling loop:
```
messages = [system_prompt, ...conversation, user_message]
while rounds < MAX_TOOL_ROUNDS:
    response = ollama.chat(model, messages, tools=tool_list, think=False)
    if no tool_calls: return response.content
    for each tool_call: execute it, append a {"role": "tool", ...} result
```
- `MAX_TOOL_ROUNDS = 20` — was `8` originally, raised after live testing
  showed a genuinely broad multi-course query (7 courses × 1 tool call each
  + 1 synthesis round = 8) exactly exhausted the original cap before the
  model could produce a final answer.
- `think=False` — Qwen 3 supports an explicit reasoning/"thinking" mode;
  `think=True` was tried live specifically to fix an under-commitment
  problem (see bugs below) and made it *worse* (higher latency, no more
  follow-through, sometimes stopped to ask an unnecessary clarifying
  question). The actual fix was strengthening `system_prompt.py` instead;
  `think` was reverted to `False`.
- `_execute_tool()` never lets a tool call crash the loop: `LearnAuthenticationError`
  → typed `{"error": true, "type": "authentication_expired", ...}` dict (not
  `[]`); `LearnApiError` → `"api_error"`; a `TypeError` (wrong/missing
  arguments from the model) → `"tool_error"`; any other exception →
  `"tool_error"` as a last resort. All of these are appended as normal tool
  results so the model sees them and can react, per the system prompt's
  explicit "an error is not empty data" rule.
- Debug mode (`--debug`) prints only `name(args)` and a result-size summary
  (`"returned N item(s)"` or `"error: ..."`) — never the raw result content,
  so no rich-text data or (theoretically) sensitive info can leak into the
  visible log. Covered by test (`test_debug_mode_does_not_print_raw_result_only_summary`).

### `agent/system_prompt.py`
This has gone through several real iterations, each driven by a specific
live-testing failure — not written speculatively. See
[Bugs found during live testing](#bugs-found-during-live-testing-and-their-fixes)
for the story behind each rule. The current version (see the file itself for
the literal text — it's long) covers, in order: core anti-fabrication
principles, course identification rules (never pass a code/name as
`course_id`), multi-course follow-through (don't stop to narrate or ask
permission for breadth alone), per-course accounting in summaries (never
silently drop a course with zero results), the announcement-fallback rule
for due dates D2L only exposes as free text, grade-calculation restrictions
(no naive point-summing for an "overall grade"), submission-status
restrictions (no such field exists — don't imply one), truncation/detail-tool
awareness, error-vs-empty-result distinction, and tool-call self-correction
(retry immediately with fixed arguments, don't just narrate about retrying).

### `agent.py`
The single CLI entry point. `_model_arg()` pulls `--model <value>` out of
argv and `agent/backend.py` resolves it to a `Backend` (name, model,
client-construction, connectivity check, exception type) before anything
else runs — the rest of `agent.py` doesn't know or care which LLM backend
was picked. `run_logout()` implements the `logout` subcommand (just calls
`browser.clear_session()`). `run_test_llm()` implements `--test-llm`
(LLM-only, no LEARN). `run_interactive_agent(backend, debug)` does the full
flow: check the resolved backend → launch browser → auth (with optional
credential auto-fill) → `LearnClient` → `build_tools()` → REPL loop calling
`agent/loop.py`'s `run_turn()` per user message, maintaining `messages` as
ongoing history across turns within one process run (no persistence across
runs, and no mid-conversation backend switching — the backend is fixed for
the whole run).

---

## Key engineering decisions, and why

- **`launch_persistent_context`, not `launch` + `new_context`.** The whole
  "don't re-authenticate every run" property depends on this one choice —
  it's what makes `.auth/browser-profile/` a real, reusable Chrome profile
  rather than throwaway in-memory state.
- **Cookie-based Valence auth via `context.request`, not a registered
  OAuth/ID-Key application.** This wasn't a shortcut — it was investigated
  explicitly (see the architecture audit). Registering a real Valence
  application requires Waterloo IT/Brightspace admin approval, which an
  individual student generally can't self-serve. Riding the same cookies
  LEARN's own frontend JS uses was the only practical option, and it works
  because `context.request` genuinely shares the browser's cookie jar
  (verified, not assumed).
- **Normalization lives in `learn/`, LLM-safety concerns live in `agent/`.**
  Two different boundaries for two different reasons: `learn/models.py`
  turns D2L's shape into a stable Python interface for *any* consumer;
  `agent/tools.py` additionally strips `raw` and truncates rich text
  because *this specific consumer* (an 8B model with a finite context
  window) has a constraint no one else using this codebase has.
  Conflating the two would degrade data for every non-agent consumer for no
  reason.
- **Typed exceptions (`LearnAuthenticationError` etc.) instead of return
  values.** Found via an explicit architecture audit that flagged
  "authentication expired → `[]`" as a real correctness bug (the agent
  would tell the user "you have no assignments" instead of "please log in
  again"). Fixed once, at the `LearnClient._get()` layer, so every caller
  benefits without having to remember to check for it themselves.
- **Item counts are never capped; rich-text length is.** An explicit user
  decision after the original "cap `get_announcements()` to the most recent
  N" proposal was rejected — the chosen fix addresses the actual problem
  (huge total text volume) without silently hiding older announcements a
  user might still care about.
- **`think=False`, with the reasoning problem solved in the prompt instead.**
  `think=True` was tried and made things worse on every axis that mattered
  (latency, follow-through, unnecessary clarifying questions). This is
  recorded so nobody re-tries the same "just turn thinking on" fix expecting
  a different result without a reason to believe it'll behave differently.
- **`MAX_TOOL_ROUNDS` exists at all.** Any agent loop that lets a model
  decide when to stop calling tools needs a hard ceiling, or a
  model that gets stuck in a call/error/retry cycle runs forever. 20 was
  chosen empirically (enough for a real 7-course sweep + synthesis), not
  as a round number.
- **`agent/tools.py` functions are defined as closures inside `build_tools(client)`**
  rather than as free functions taking `client` as a parameter. This lets
  them be passed directly to `ollama`'s `tools=` argument (which introspects
  a plain callable's signature/docstring for the schema) while still being
  bound to one authenticated client — no global state, no `functools.partial`
  boilerplate.

---

## Bugs found during live testing (and their fixes)

These were all found by actually running the system against a real Waterloo
account, not by inspection — several contradicted what seemed like
reasonable assumptions going in. Recorded in detail so nobody "fixes" them
back to the broken version by accident.

1. **`page.is_closed()` doesn't detect a killed browser *process*, only a
   closed *tab*.** Closing the entire Chromium window (not just switching
   away) hung `wait_for_authentication()` forever, because the DOM-check
   exception it caused was silently swallowed as "not authenticated yet."
   **Fix:** listen for the `BrowserContext`'s own `"close"` event instead of
   inferring closure from exception types.

2. **`page.url` can go permanently stale mid-session.** During a genuine
   first-time login (WatIAM → ADFS → Duo → back to LEARN), `page.url` was
   observed to freeze on the WatIAM URL forever — confirmed with per-tick
   debug logging showing dozens of identical reads with zero exceptions,
   while the browser had visibly, successfully reached LEARN on screen.
   Reused/already-authenticated sessions (single same-origin load, no
   multi-hop federation redirect) never triggered it. **Fix:**
   `current_url()` reads `window.location.href` via `page.evaluate()` — a
   real round trip into the renderer — instead of trusting Playwright's
   CDP-event-driven cache.

3. **`get_announcements()` across 4 courses (110 items, full HTML bodies)
   made a single agent turn hang.** Not infinite — just extremely slow
   local inference on an enormous prompt, with no timeout configured to
   surface that clearly. **Fix:** HTML-stripped, 500-char-truncated
   rich-text fields in `agent/tools.py` (item counts stay uncapped, per
   explicit decision), plus `get_announcement_detail()` as an escape hatch.

4. **The model stopped after one tool call and just narrated a plan** for
   genuinely broad questions ("gather info about all my final exams") —
   e.g. "Let me start with EARTH 123..." with no further tool call, which
   the loop correctly (but unhelpfully) treated as the final answer.
   **Tried and rejected:** `think=True` — made it worse (slower, and once
   asked an unnecessary clarifying question instead of proceeding).
   **Actual fix:** an explicit system-prompt rule that a request spanning
   multiple courses is not ambiguous and must not be treated as a stopping
   point.

5. **`MAX_TOOL_ROUNDS=8` was too low** once fix #4 made the model actually
   follow through — a 7-course sweep (1×`get_courses` + 7×`get_assignments`)
   used the entire cap with zero rounds left to synthesize an answer, so it
   hit the generic "wasn't able to finish" fallback. **Fix:** raised to 20.

6. **The final summary silently dropped one course (MSE 263) that had zero
   results**, while correctly mentioning two *other* zero-result courses
   ("have no assignments listed"). The tool call for that course was
   correct and returned the correct empty result — this was purely an
   inconsistency in the model's own synthesis. **Fix:** explicit
   system-prompt rule to account for every course checked, even ones with
   nothing found.

7. **`get_courses()` was called with a hallucinated `{'filter': 'MSE 263'}`
   argument** when a query already named a specific course — reproduced
   twice, consistently, not sampling noise. Caught cleanly by existing
   `TypeError` handling in `agent/loop.py` (no crash), but the model then
   just narrated "let me try again" without actually retrying.
   **Two-part fix:** (a) `get_courses()`'s docstring now explicitly states
   it takes no arguments; (b) an explicit system-prompt rule that a
   `tool_error`/`unknown_tool` result means *your* mistake, and the
   corrected call must happen immediately, not be described.

8. **MSE 263's due dates only exist as free text inside announcements**,
   not through the Dropbox/assignments API at all (`get_assignments()`
   legitimately, correctly returns `0` items for that course). Before a
   fix, asking "what's due in MSE 263" got a flat "no assignments found" —
   technically true of the structured data, practically useless.
   **Fix:** a system-prompt rule to fall back to `get_announcements()` when
   `get_assignments()` returns little/nothing, and to explicitly flag such
   dates as coming from announcement text rather than structured data.
   **Known-incomplete part of this fix:** the model reliably performs the
   *fallback* now (verified live, twice, across two different prompt
   phrasings), but has not yet reliably added the *provenance caveat* even
   though both prompt phrasings asked for it explicitly — see
   [Known limitations](#known-limitations--open-items).

9. **The Node.js driver process Playwright spawns underneath can log an
   `EPIPE` error when a Python process holding it is killed abruptly**
   (e.g. via a background-task stop). This is expected cleanup noise from
   an ungraceful shutdown, not a real bug — noted here only so it isn't
   mistaken for one if seen again.

10. **Early hallucination risk in agent answers**, found on the very first
    real end-to-end agent test: the model invented "Status: Submitted/Not
    yet submitted" (no such field exists — `Assignment` deliberately has no
    submission-status field, see `learn/models.py`) and a computed
    "Overall Grade: 91.45%" (a naive point-sum across grade items with
    different real weights — exactly the kind of misleading calculation
    the grade model was designed to avoid producing). **Fix:** explicit
    system-prompt rules forbidding both; verified live that both are gone
    after the fix.

---

## Testing, in depth

```bash
python -m pytest
```

177 tests, all offline — no live LEARN login, no live Ollama server, no
Gemini API key, no network access at all. Three fake layers make this possible:
- `tests/fake_valence.py` — a fake Playwright `APIRequestContext` whose
  `.get(url)` returns a scripted `(status, body)` pair from a `routes`
  dict, so `LearnClient` can be tested against real-shaped (but synthetic)
  JSON fixtures in `tests/fixtures/`.
- `tests/fake_ollama.py` — a fake `ollama.Client` whose `.chat()` pops a
  pre-scripted response off a list, so `agent/loop.py`'s tool-calling
  mechanics (including multi-round sequences and every typed-error path)
  can be tested without a real model.
- `tests/fake_gemini.py` — the same idea for `google.genai.Client`, proving
  `GeminiClient` produces the exact response shape `agent/loop.py` expects
  and that `run_turn()` works identically against both backends.

**What's covered:** ISO datetime parsing, every `LearnClient` method against
normal/empty/malformed fixture responses, all three exception types (401,
403, generic error, and the defensive 200-with-error-body path), tool
serialization (raw exclusion, datetime conversion, HTML stripping,
truncation, the detail-tool's full-text path), and the agent loop (no-tool
response, single/multi-round tool calls, unknown tool, every typed error
converted correctly instead of crashing or looking like `[]`, empty results
not mistaken for errors, the `MAX_TOOL_ROUNDS` guard, and no secret leakage
in debug output), plus the Discord adapter (auth/DM gating, rate limiting,
message chunking, and the markdown-table-to-code-block conversion).

**What's NOT covered by automated tests, and can't be without a live LLM:**
whether the model actually follows the system prompt's behavioral rules
(course identification, multi-course follow-through, per-course accounting,
the announcement-fallback rule, tool-error self-correction, the
provenance-caveat rule that's currently not reliably followed). All of
these were verified through live, manual, repeated testing against a real
account and a real running Qwen model — see the bugs list above for exactly
what was tried and what the actual live output was. If you change the
system prompt, **you must re-verify behaviorally, live** — a passing test
suite says nothing about whether the model still behaves correctly, since
none of these tests exercise real model reasoning (for either backend).

---

## Known limitations / open items

- **The announcement-provenance caveat is not reliably followed.** The
  agent correctly falls back to announcements for due dates D2L doesn't
  expose structurally (verified live, twice), but has not reliably added
  the "this came from an announcement, not structured data" disclaimer the
  system prompt explicitly asks for, across two different prompt phrasings.
  Suspected cause: an 8B model with `think=False` reliably tracking a
  secondary meta-instruction on top of answering the substantive question.
  Two untried options: (a) `think=True` scoped only to this specific
  fallback path (accept latency there specifically, since it's rarer than
  a routine query), or (b) make the caveat structural rather than
  model-remembered — e.g. a `"source": "announcement_text_not_structured"`
  marker on the data itself rather than relying on the model to say so.
- **No true "overall/final course grade" is available anywhere** — by
  design (see `learn/models.py`), not an oversight. The agent correctly
  says so rather than computing one, after an early hallucination was
  caught and fixed (see bugs list).
- **403 ambiguity is unresolvable with current evidence:** `LearnToolUnavailableError`
  covers both "tool disabled for this course" and "you don't have
  permission" — Valence's response shape doesn't let `LearnClient`
  distinguish them.
- **`Quiz.due_at` doesn't exist** — the live response needed to confirm
  the field name was truncated mid-investigation and never re-verified.
  Would need one more live authenticated fetch of a single quiz's full JSON
  to add safely.
- **No conversation persistence across process runs** — `agent.py`'s
  `messages` list only lives for one run; exiting and restarting starts
  fresh.
- **No streaming output** — a Qwen response only appears once fully
  generated, which can be 15-30+ seconds on modest local hardware,
  especially for multi-round tool-calling turns.
- **The iMessage-agent end goal is superseded by the Discord adapter** —
  see the next section; Discord DMs are the actual shipped unattended
  entry point, not iMessage.

---

## How to continue this project

The natural next milestones, roughly in order of how they were being
discussed before this document was written:

1. **Resolve the announcement-provenance caveat** (see above) — smallest
   remaining loose end from the current agent milestone.
2. **Fix 1 from the original agent-robustness plan, never actually
   implemented:** add a request timeout to the Ollama client
   (`agent/llm.py::make_client()` currently has none) with a clear
   "the model timed out" message instead of an indefinite hang — the
   truncation fix (bug #3 above) addressed the specific case that caused
   the original hang, but no general timeout safety net exists yet.
3. **Unattended/Discord-driven operation** (the actual stated end goal of
   this project — see `discord_adapter/` and `discord_bot.py`). Two real
   constraints established through direct investigation, not assumption:
   - Duo's browser-trust cookie (`browsertrust` on `api-*.duosecurity.com`)
     genuinely persists across sessions — confirmed live, multiple times,
     across real gaps of time, with zero Duo prompts as long as
     `.auth/browser-profile/` survives. Actual enforced duration is
     unknown (cookie's technical expiry was ~1 month out when checked, but
     Duo admin policy could cap it shorter).
   - **WatIAM's own ADFS session is the harder blocker, not Duo** — it was
     directly observed to expire on a shorter cycle, requiring the
     password again while Duo stayed trusted. `learn/credentials.py`
     exists specifically to let this be automated (opt-in), but Duo
     approval, whenever it *is* required, cannot be automated by design and
     needs a human-in-the-loop notification mechanism that doesn't exist
     yet.
4. **Extend the tool layer** if new use cases need it — `get_discussion_forums()`
   already exists in `LearnClient` but isn't exposed as an agent tool (no
   `Forum`/`Discussion` domain model was ever requested). Would need a
   model + tool + system-prompt update, following the same pattern as
   every other tool.

**If you're an agent picking this up cold:** read
[Bugs found during live testing](#bugs-found-during-live-testing-and-their-fixes)
before touching `learn/auth.py` or `agent/system_prompt.py` — nearly
everything non-obvious in those two files exists because of a specific,
reproduced live failure, not speculation. Don't revert `current_url()` to
`page.url`, don't recreate the `is_closed()`-only browser-closure check,
and don't change the system prompt without live-retesting the exact
scenario that motivated whatever rule you're touching — the test suite
will still pass either way, since none of it exercises real model
reasoning.
