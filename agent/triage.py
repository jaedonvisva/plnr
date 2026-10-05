"""Announcement triage: which announcements actually need the student's attention.

A small local decision model (Laya, optional install) answers three yes/no
questions per announcement; plain Python combines them into one score. The chat
LLM then sees a short ranked list instead of every announcement in every course.
See docs/superpowers/specs/2026-10-05-announcement-triage-design.md.

The decision model never generates text and never sees anything but one
announcement's title + stripped body, so announcement content cannot steer tool
behavior -- its output is three probabilities. Dates are deliberately not
judged by the model (they're unreliable at that); deadlines stay deterministic
elsewhere (agent/aggregation.py).

Every cross-course result here has the same shape as agent/aggregation.py's:
courses checked, courses with no results, and per-course errors are always
reported, so a course can never silently drop out of an answer.
"""

from __future__ import annotations

import importlib.util
import math
from typing import Any, Protocol

from learn.client import LearnClient
from learn.exceptions import LearnApiError
from learn.models import Announcement, Course

from . import aggregation
from .serialize import serialize, strip_html

MODEL_BODY_CHARS = 1200
"""Laya's English checkpoint has ~320 tokens of state budget (~4 chars/token).
Longer bodies are cut for the model only and flagged, never silently."""

TEMPERATURE = 1.0
"""Laya ships over-confident. Fit this from scripts/eval_triage.py on labeled
announcements; 1.0 means 'trust the raw probabilities'."""

ROUTINE_PENALTY = 0.5

QUESTIONS: dict[str, dict[str, str]] = {
    "schedule_change": {
        "type": "noul",
        "instructions": "Does this announcement change a deadline, exam, or location?",
    },
    "action_required": {
        "type": "noul",
        "instructions": "Does this announcement require the student to do something?",
    },
    "routine": {
        "type": "noul",
        "instructions": "Is this announcement routine or administrative only, with nothing new to act on?",
    },
}

SOURCE = "local_classifier_estimate"


class Decider(Protocol):
    def decide(self, state: dict[str, str], questions: dict[str, dict[str, str]]) -> dict[str, float]:
        """Return a yes-probability in [0, 1] for every question id."""
        ...


class LayaDecider:
    """Adapter over `laya.Router`. Imports laya and loads weights lazily, on the
    first decide() call, so constructing one (and registering the tool) is free."""

    def __init__(self) -> None:
        self._router: Any = None

    def decide(self, state: dict[str, str], questions: dict[str, dict[str, str]]) -> dict[str, float]:
        if self._router is None:
            from laya import Router

            self._router = Router()
        answers = self._router.predict(state, questions)["answers"]
        return {qid: float(answers[qid]["noul"]) for qid in questions}


def default_decider() -> Decider | None:
    """A LayaDecider if the `laya` package is installed, else None (no triage
    tool). Checks availability without importing laya, which pulls in torch."""
    try:
        available = importlib.util.find_spec("laya") is not None
    except ValueError:  # already in sys.modules without a __spec__
        available = True
    return LayaDecider() if available else None


def calibrate(p: float, temperature: float = TEMPERATURE) -> float:
    """Temperature-scale a probability: sigmoid(logit(p) / T)."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    logit = math.log(p / (1 - p))
    return 1 / (1 + math.exp(-logit / temperature))


def combine_score(signals: dict[str, float]) -> float:
    """importance = max(schedule_change, action_required), dampened by `routine`."""
    importance = max(signals["schedule_change"], signals["action_required"])
    return importance * (1 - ROUTINE_PENALTY * signals["routine"])


def _model_state(announcement: Announcement) -> tuple[dict[str, str], bool]:
    body = strip_html(announcement.body) if announcement.body else ""
    truncated = len(body) > MODEL_BODY_CHARS
    return {"title": announcement.title, "body": body[:MODEL_BODY_CHARS]}, truncated


def triage_announcements(
    client: LearnClient,
    courses: list[Course],
    decider: Decider,
    course_id: str | None,
    min_score: float,
    limit: int,
) -> dict[str, Any]:
    selected = aggregation._select_courses(courses, course_id)
    if isinstance(selected, dict):
        return selected

    scored: list[dict[str, Any]] = []
    courses_with_no_results: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for course in selected:
        try:
            announcements = client.get_announcements(course.id)
        except LearnApiError as exc:
            errors.append({**aggregation._course_ref(course), "message": str(exc)})
            continue

        kept = 0
        for announcement in announcements:
            state, truncated = _model_state(announcement)
            raw_signals = decider.decide(state, QUESTIONS)
            signals = {qid: calibrate(float(raw_signals[qid])) for qid in QUESTIONS}
            score = combine_score(signals)
            if score < min_score:
                continue
            kept += 1
            scored.append(
                {
                    **serialize(announcement),
                    "score": round(score, 4),
                    "signals": {qid: round(p, 4) for qid, p in signals.items()},
                    "body_truncated_for_model": truncated,
                    "source": SOURCE,
                }
            )
        if kept == 0:
            courses_with_no_results.append(aggregation._course_ref(course))

    scored.sort(key=lambda r: r["score"], reverse=True)

    return {
        "min_score": min_score,
        "courses_checked": [aggregation._course_ref(c) for c in selected],
        "results": scored[:limit],
        "courses_with_no_results": courses_with_no_results,
        "errors": errors,
        "note": "Scores are local-model estimates of importance, not LEARN facts. "
        "Read the announcement text before stating anything as certain.",
    }
