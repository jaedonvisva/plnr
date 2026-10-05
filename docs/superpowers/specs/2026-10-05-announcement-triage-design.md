# Announcement triage (local Laya classifier) — design

## Goal

Answer "is there anything I need to act on?" without the chat LLM reading every
announcement in every course. A new read-only tool returns only announcements
that matter, ranked, scored by a small local decision model
([Laya](https://huggingface.co/convaiinnovations/laya), Apache 2.0, runs on-device).

Today the LLM has `get_announcements` (everything, truncated) and
`search_announcements` (substring match, `agent/aggregation.py`). Neither can
answer an open-ended importance question.

## Scope

In: `agent/triage.py`, tool `get_important_announcements`, optional `triage`
install extra, an offline eval script + labeled fixtures, tests, docs.

Out (explicitly): proactive Discord digest, Jev backend, semantic search
reranking, confidence gating of answers. The triage function is written so a
digest can call it later.

## Success criteria

- LLM can call one tool and receive a short ranked list instead of all announcements.
- Tool is absent (not erroring) when `laya` isn't installed; the default install is unchanged.
- Offline test suite stays offline and green; Laya is never imported by tests.
- Accuracy is measured on the user's own labeled announcements (eval script), and
  the default threshold comes from that eval.

## Design

**`Decider` protocol** — `decide(state: dict, questions: dict) -> dict[str, float]`,
returning a yes-probability per question id. `LayaDecider` is the only real
implementation: lazy-imports `laya`, builds `laya.Router()` once on first use,
reads `result["answers"][qid]["noul"]`. Swapping in another backend later is one class.

**Questions** (all `noul`, per announcement, judged in isolation):
- `schedule_change` — changes a deadline, exam, or location?
- `action_required` — requires the student to do something?
- `routine` — routine/administrative only?

**Scoring** is plain Python, deterministic, and testable:
`importance = max(schedule_change, action_required)`;
`score = importance * (1 - 0.5 * routine)`. A single `TEMPERATURE` constant
(default 1.0) rescales probabilities (`sigmoid(logit(p)/T)`) because Laya ships
over-confident; it is fitted from the eval, not guessed. Dates are never judged by
the model.

**Data flow** — same shape as the other cross-course tools: select courses
(`aggregation._select_courses`), fetch announcements per course, score each, keep
`score >= min_score`, sort descending, cap at `limit`. Result carries
`courses_checked`, `courses_with_no_results`, `errors`, `results`. A failing course
goes to `errors`; the others continue. Each result is the normal serialized
announcement (HTML stripped, preview-truncated, untrusted marker) plus `score`,
`signals` (the three probabilities), `body_truncated_for_model`, and a `source`
field stating the score is a local model estimate, not a LEARN fact.

**Model input** — title + HTML-stripped body, cut to `MODEL_BODY_CHARS` (~1200,
Laya English budget is ~320 tokens). Items cut short are flagged.

**Tool** — `get_important_announcements(course_id=None, min_score=0.5, limit=10)`;
validated like the other tools (known course, `min_score` a float in [0,1], `limit`
an int in [1,50], bools rejected). `build_tools(client, decider=None)` registers it
only when a decider is passed; entry points pass `triage.default_decider()` which
returns `None` unless `laya` is importable. Deterministic tests are therefore
independent of what is installed.

**Prompt** — `SYSTEM_PROMPT` gains a short rule: if `get_important_announcements`
is among your tools, prefer it for open-ended "anything important/urgent" questions;
treat scores as estimates and never state them as verified facts.

## Security

Read-only, no new write path. Model output is numbers only; announcement text
cannot steer tool behavior. Triage runs locally even under `--model gemini` (only
the filtered results reach the chat LLM, as with every other tool). README security
section notes this.

## Errors

- `laya` missing → tool not registered, no crash.
- Decider raises → tool returns `{"error": True, "type": "triage_unavailable", ...}`.
- One course fails → listed in `errors`, others still scored.

## Testing

`FakeDecider` with canned probabilities: scoring/ordering, threshold, limit, failing
course, argument validation, tool-absence without a decider, `LayaDecider` against a
stub `laya` module injected into `sys.modules`. Existing tests unchanged.
`scripts/eval_triage.py` (manual, needs `laya`) reports accuracy per threshold on
`tests/fixtures/triage_labeled.json`.
