"""Deterministic, offline-testable cross-course aggregation and search.

This is where Python does the work Qwen shouldn't have to: date-window
filtering, text search, sorting, and per-course bookkeeping. Nothing here
touches Ollama -- every function takes plain LearnClient/domain-model data
and returns plain JSON-safe dicts, so it's testable with FakeLearnClient-style
fakes exactly like learn/client.py already is (see tests/fake_valence.py).

Every cross-course function returns the same shape (see _cross_course_result
below): which courses were checked, which had no matches, which errored, and
the matches themselves. This makes it structurally hard for the LLM to
silently drop a course from its answer -- the omission would have to be
visible in the tool result itself, not just absent from it.

Provenance: every item returned here carries a "source" field describing
where the data came from (e.g. "structured_assignment_data"), per the
project's rule that the model must not blur "verified LEARN field" with
"inferred from free text." Nothing here invents a due date or any other
field -- if the underlying model doesn't have it, it isn't included.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from learn.client import LearnClient
from learn.exceptions import LearnApiError
from learn.models import Course

from .serialize import UNTRUSTED_MARKER_KEY, UNTRUSTED_MARKER_VALUE, serialize, strip_html

WATERLOO_TZ = ZoneInfo("America/Toronto")
DEFAULT_UPCOMING_WINDOW_DAYS = 14

QUIZ_DUE_DATE_NOTE = (
    "Quizzes are included only when LEARN gives them a structured DueDate; "
    "a quiz without one is NOT listed here, so absence is not proof nothing is "
    "due. Use get_quizzes(course_id) to list every quiz in a course."
)


def now_local() -> datetime:
    """The real current time, Waterloo-local (America/Toronto). Callers must
    use this instead of asking the model what 'today' is -- Qwen has no
    reliable notion of the current date."""
    return datetime.now(WATERLOO_TZ)


def _course_ref(course: Course) -> dict[str, str]:
    return {"course_id": course.id, "course_name": course.name}


def _select_courses(courses: list[Course], course_id: str | None) -> list[Course] | dict[str, Any]:
    """Resolve which courses a cross-course tool should operate over.

    Returns a list of courses to search, or an error dict if course_id was
    given but doesn't match any known course (never silently searches zero
    courses in that case).
    """
    if course_id is None:
        return [c for c in courses if c.is_active]
    matches = [c for c in courses if c.id == course_id]
    if not matches:
        return {
            "error": True,
            "type": "not_found",
            "message": f"No course with id {course_id!r}. Call get_courses() first to find the right id.",
        }
    return matches


def collect_upcoming_assignments(
    client: LearnClient,
    courses: list[Course],
    days_ahead: int,
    now: datetime,
) -> dict[str, Any]:
    """Gather assignments and quizzes across active courses whose structured
    `due_at` falls within [now, now + days_ahead]. A quiz is only included when
    LEARN provides a DueDate for it -- see QUIZ_DUE_DATE_NOTE. Errors fetching
    one course's data don't abort the others; they're reported per-course in
    "errors".
    """
    active_courses = [c for c in courses if c.is_active]
    window_end = now + timedelta(days=days_ahead)

    items: list[dict[str, Any]] = []
    courses_with_no_results: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for course in active_courses:
        try:
            assignments = client.get_assignments(course.id)
            quizzes = client.get_quizzes(course.id)
        except LearnApiError as exc:
            errors.append({**_course_ref(course), "message": str(exc)})
            continue

        matches = [a for a in assignments if a.due_at is not None and now <= a.due_at <= window_end]
        quiz_matches = [q for q in quizzes if q.due_at is not None and now <= q.due_at <= window_end]
        if not matches and not quiz_matches:
            courses_with_no_results.append(_course_ref(course))
            continue

        for q in quiz_matches:
            items.append(
                {
                    UNTRUSTED_MARKER_KEY: UNTRUSTED_MARKER_VALUE,  # q.name is instructor-controlled text
                    "type": "quiz",
                    **_course_ref(course),
                    "id": q.id,
                    "name": q.name,
                    "due_at": q.due_at.isoformat(),
                    "is_active": q.is_active,
                    "source": "structured_quiz_data",
                }
            )

        for a in matches:
            items.append(
                {
                    UNTRUSTED_MARKER_KEY: UNTRUSTED_MARKER_VALUE,  # a.name is instructor-controlled text
                    "type": "assignment",
                    **_course_ref(course),
                    "id": a.id,
                    "name": a.name,
                    "due_at": a.due_at.isoformat(),
                    "points_earned": a.points_earned,
                    "points_possible": a.points_possible,
                    "source": "structured_assignment_data",
                    "verified_structured_assignment": True,
                }
            )

    items.sort(key=lambda i: i["due_at"])

    return {
        "window": {"from": now.isoformat(), "to": window_end.isoformat(), "days_ahead": days_ahead},
        "courses_checked": [_course_ref(c) for c in active_courses],
        "items": items,
        "courses_with_no_results": courses_with_no_results,
        "errors": errors,
        "note": QUIZ_DUE_DATE_NOTE,
    }


def search_announcements(
    client: LearnClient,
    courses: list[Course],
    query: str,
    course_id: str | None,
) -> dict[str, Any]:
    """Search announcement title + full body (before truncation) for `query`
    (case-insensitive substring match), across one course or all active
    courses. Matches are returned truncated the same way get_announcements()
    truncates them; use get_announcement_detail() for full text.
    """
    selected = _select_courses(courses, course_id)
    if isinstance(selected, dict):
        return selected

    query_lower = query.strip().lower()
    results: list[dict[str, Any]] = []
    courses_with_no_results: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for course in selected:
        try:
            announcements = client.get_announcements(course.id)
        except LearnApiError as exc:
            errors.append({**_course_ref(course), "message": str(exc)})
            continue

        matches = [
            a
            for a in announcements
            if query_lower in a.title.lower() or (a.body and query_lower in strip_html(a.body).lower())
        ]
        if not matches:
            courses_with_no_results.append(_course_ref(course))
            continue

        for a in matches:
            results.append(serialize(a))

    return {
        "query": query,
        "courses_checked": [_course_ref(c) for c in selected],
        "results": results,
        "courses_with_no_results": courses_with_no_results,
        "errors": errors,
    }


def search_course_content(
    client: LearnClient,
    courses: list[Course],
    query: str,
    course_id: str | None,
) -> dict[str, Any]:
    """Search course content item titles + descriptions for `query`
    (case-insensitive substring match), across one course or all active
    courses.
    """
    selected = _select_courses(courses, course_id)
    if isinstance(selected, dict):
        return selected

    query_lower = query.strip().lower()
    results: list[dict[str, Any]] = []
    courses_with_no_results: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for course in selected:
        try:
            content_items = client.get_course_content(course.id)
        except LearnApiError as exc:
            errors.append({**_course_ref(course), "message": str(exc)})
            continue

        matches = [
            item
            for item in content_items
            if query_lower in item.title.lower()
            or query_lower in item.module_title.lower()
            or (item.description and query_lower in strip_html(item.description).lower())
        ]
        if not matches:
            courses_with_no_results.append(_course_ref(course))
            continue

        for item in matches:
            results.append(serialize(item))

    return {
        "query": query,
        "courses_checked": [_course_ref(c) for c in selected],
        "results": results,
        "courses_with_no_results": courses_with_no_results,
        "errors": errors,
    }
