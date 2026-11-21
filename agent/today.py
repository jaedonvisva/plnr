"""A deterministic, LLM-free "what's due" view and iCalendar export.

Everything here is plain Python over LearnClient data: no model decides what is
due. Inspired by agent2learn's `today` / `calendar` commands
(https://github.com/ManagementMO/agent2learn, Apache-2.0).

Honesty constraints (same as the rest of the project):
  - Only structured due dates are used. Dates that exist only as announcement
    text are not guessed at.
  - LEARN exposes no submission status to students, so "overdue" can only mean
    "past due and not yet graded" -- it is labelled that way, never "missing".
  - A course that failed to load is reported in `errors`, never silently
    treated as "nothing due".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from learn.client import LearnClient
from learn.exceptions import LearnApiError
from learn.models import Course

from .aggregation import WATERLOO_TZ

DEFAULT_UPCOMING_DAYS = 7
DEFAULT_OVERDUE_DAYS = 14
EVENT_BLOCK = timedelta(minutes=30)
"""Deadlines are exported as a 30-minute block that ENDS at the due time, so the
calendar entry visibly finishes when the work is due."""

OVERDUE_NOTE = (
    "'Past due' means the due date has passed and no grade is recorded yet. LEARN "
    "does not expose whether you submitted, so this is not proof anything is missing."
)


@dataclass(frozen=True)
class Deadline:
    uid_source: str
    course_id: str
    course_name: str
    course_label: str
    kind: str  # "assignment" | "quiz"
    source_id: str
    name: str
    due_at: datetime
    graded: bool = False


def _label(course: Course) -> str:
    return course.display_code or course.name


def collect_deadlines(
    client: LearnClient, courses: list[Course]
) -> tuple[list[Deadline], list[dict[str, str]], list[Course]]:
    """Every dated assignment/quiz in the active courses, plus per-course errors."""
    active = [c for c in courses if c.is_active]
    deadlines: list[Deadline] = []
    errors: list[dict[str, str]] = []
    for course in active:
        try:
            assignments = client.get_assignments(course.id)
            quizzes = client.get_quizzes(course.id)
        except LearnApiError as exc:
            errors.append({"course_id": course.id, "course_name": course.name, "message": str(exc)})
            continue
        for a in assignments:
            if a.due_at is None or a.is_hidden:
                continue
            deadlines.append(
                Deadline(
                    uid_source=f"assignment:{course.id}:{a.id}",
                    course_id=course.id,
                    course_name=course.name,
                    course_label=_label(course),
                    kind="assignment",
                    source_id=a.id,
                    name=a.name,
                    due_at=a.due_at,
                    graded=a.points_earned is not None,
                )
            )
        for q in quizzes:
            if q.due_at is None:
                continue
            deadlines.append(
                Deadline(
                    uid_source=f"quiz:{course.id}:{q.id}",
                    course_id=course.id,
                    course_name=course.name,
                    course_label=_label(course),
                    kind="quiz",
                    source_id=q.id,
                    name=q.name,
                    due_at=q.due_at,
                )
            )
    deadlines.sort(key=lambda d: (d.due_at, d.course_label, d.name))
    return deadlines, errors, active


def _row(d: Deadline, now: datetime) -> dict[str, Any]:
    local = d.due_at.astimezone(WATERLOO_TZ)
    delta = d.due_at - now
    return {
        "course": d.course_label,
        "course_name": d.course_name,
        "kind": d.kind,
        "name": d.name,
        "due_at": d.due_at.isoformat(),
        "due_local": local.strftime("%a %b %d, %I:%M %p").replace(" 0", " "),
        "in_hours": round(delta.total_seconds() / 3600, 1),
    }


def build_today(
    deadlines: list[Deadline],
    now: datetime,
    *,
    upcoming_days: int = DEFAULT_UPCOMING_DAYS,
    overdue_days: int = DEFAULT_OVERDUE_DAYS,
) -> dict[str, list[dict[str, Any]]]:
    """Bucket deadlines relative to `now` (tz-aware).

    - past_due: assignments due within the last `overdue_days` with no grade yet
    - today: still ahead, and falling on today's Waterloo-local date
    - upcoming: later than today, within `upcoming_days`
    """
    today_local = now.astimezone(WATERLOO_TZ).date()
    horizon = now + timedelta(days=upcoming_days)
    past_due: list[dict[str, Any]] = []
    today: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    for d in deadlines:
        if d.due_at < now:
            if d.kind == "assignment" and not d.graded and d.due_at >= now - timedelta(days=overdue_days):
                past_due.append(_row(d, now))
            continue
        if d.due_at.astimezone(WATERLOO_TZ).date() == today_local:
            today.append(_row(d, now))
        elif d.due_at <= horizon:
            upcoming.append(_row(d, now))
    return {"past_due": past_due, "today": today, "upcoming": upcoming}


def render_today(buckets: dict[str, list[dict[str, Any]]], errors: list[dict[str, str]], now: datetime) -> str:
    def block(title: str, rows: list[dict[str, Any]]) -> list[str]:
        if not rows:
            return [f"{title}: nothing"]
        out = [f"{title}:"]
        out += [f"  • [{r['course']}] {r['name']} ({r['kind']}) — {r['due_local']}" for r in rows]
        return out

    lines = [f"Today is {now.astimezone(WATERLOO_TZ).strftime('%A, %B %d').replace(' 0', ' ')} (Waterloo time)", ""]
    lines += block("Past due, not yet graded", buckets["past_due"])
    if buckets["past_due"]:
        lines.append(f"  ({OVERDUE_NOTE})")
    lines += [""] + block("Due today", buckets["today"])
    lines += [""] + block("Coming up", buckets["upcoming"])
    for e in errors:
        lines += ["", f"⚠ Could not load {e['course_name']}: {e['message']} — its deadlines are NOT shown."]
    return "\n".join(lines)


# ---- iCalendar ----------------------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545 line folding: at most 75 octets per physical line, continuation lines start
    with a single space. Never splits inside a UTF-8 character."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return [line]
    parts: list[str] = []
    current = b""
    limit = 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > limit:
            parts.append(current.decode("utf-8"))
            current = b
            limit = 74  # leading space counts toward the 75
        else:
            current += b
    parts.append(current.decode("utf-8"))
    return [parts[0]] + [" " + p for p in parts[1:]]


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def to_ics(deadlines: list[Deadline], now: datetime) -> str:
    """Serialize deadlines as a deterministic iCalendar document (CRLF lines).

    UIDs are a hash of course+item ids, so re-importing an updated export
    updates events instead of duplicating them. DTSTAMP is `now`, so the output
    is reproducible for a fixed clock. Only names and dates are exported -- no
    descriptions, announcement text or grades.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//plnr//LEARN deadlines//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:LEARN deadlines",
    ]
    for d in deadlines:
        uid = sha256(d.uid_source.encode()).hexdigest()[:32] + "@plnr"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{_utc(now)}",
            f"DTSTART:{_utc(d.due_at - EVENT_BLOCK)}",
            f"DTEND:{_utc(d.due_at)}",
            f"SUMMARY:{_escape(f'{d.course_label}: {d.name} (due)')}",
            f"CATEGORIES:{_escape(d.kind)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    out: list[str] = []
    for line in lines:
        out.extend(_fold(line))
    return "\r\n".join(out) + "\r\n"
