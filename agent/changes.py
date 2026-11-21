"""Change detection: what's new or different in LEARN since the last check.

Takes a compact snapshot of each active course (assignment/quiz dates, announcement
titles and edit times, grade values), compares it with the previous snapshot saved
on disk, and reports differences. Plain Python, no model. Idea from agent2learn's
`snapshot`/`diff` (https://github.com/ManagementMO/agent2learn, Apache-2.0).

Design rules:
  - A course that fails to load keeps its OLD snapshot, so a transient error is never
    reported as "everything was removed" and is surfaced in `errors` instead.
  - The snapshot stores no announcement bodies or comments -- titles, ids, dates and
    displayed grade strings only. It lives in `.plnr/` (gitignored, owner-only).
  - The first run has no baseline: it saves one and says so, rather than reporting
    every item as "new".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from learn.client import LearnClient
from learn.exceptions import LearnApiError, LearnAuthenticationError
from learn.fsutil import atomic_write_private
from learn.models import Course

from .serialize import UNTRUSTED_MARKER_KEY, UNTRUSTED_MARKER_VALUE

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT_PATH = PROJECT_ROOT / ".plnr" / "snapshot.json"
SCHEMA_VERSION = 1


class SnapshotStore:
    """Where the previous snapshot lives. Injectable so tests never touch the real path."""

    def __init__(self, path: Path = DEFAULT_SNAPSHOT_PATH):
        self.path = path

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None  # unreadable/corrupt baseline == no baseline
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            return None
        return data

    def save(self, snapshot: dict) -> None:
        atomic_write_private(self.path, json.dumps(snapshot, sort_keys=True, ensure_ascii=False) + "\n")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def snapshot_course(client: LearnClient, course: Course) -> dict[str, Any]:
    """Raises LearnApiError if any part can't be loaded (caller keeps the old snapshot)."""
    assignments = client.get_assignments(course.id)
    quizzes = client.get_quizzes(course.id)
    announcements = client.get_announcements(course.id)
    grades = client.get_grades(course.id)
    return {
        "name": course.name,
        "label": course.display_code or course.name,
        "assignments": {
            a.id: {
                "name": a.name,
                "due_at": _iso(a.due_at),
                "hidden": a.is_hidden,
                "graded": a.points_earned is not None,
            }
            for a in assignments
        },
        "quizzes": {
            q.id: {"name": q.name, "due_at": _iso(q.due_at), "start_at": _iso(q.start_at), "end_at": _iso(q.end_at)}
            for q in quizzes
        },
        "announcements": {
            n.id: {"title": n.title, "updated_at": _iso(n.updated_at or n.created_at)}
            for n in announcements
            if not n.is_hidden
        },
        "grades": {g.id: {"name": g.name, "displayed": g.displayed_grade} for g in grades},
    }


def take_snapshot(
    client: LearnClient, courses: list[Course], previous: dict | None, now: datetime
) -> tuple[dict, list[dict[str, str]]]:
    """New snapshot of all active courses. Failed courses carry forward their old data.
    Raises LearnAuthenticationError if the session has expired."""
    old_courses = (previous or {}).get("courses", {})
    result: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for course in (c for c in courses if c.is_active):
        try:
            result[course.id] = snapshot_course(client, course)
        except LearnAuthenticationError:
            raise  # an expired session is not a per-course problem; callers must not treat it as one
        except LearnApiError as exc:
            errors.append({"course_id": course.id, "course_name": course.name, "message": str(exc)})
            if course.id in old_courses:
                result[course.id] = old_courses[course.id]
    return {"version": SCHEMA_VERSION, "taken_at": now.isoformat(), "courses": result}, errors


_KINDS = (
    ("assignments", "assignment", "name"),
    ("quizzes", "quiz", "name"),
    ("announcements", "announcement", "title"),
    ("grades", "grade", "name"),
)


def _change(course_id: str, course: dict, kind: str, change: str, item_id: str, name: str, detail: str | None = None) -> dict:
    row = {
        UNTRUSTED_MARKER_KEY: UNTRUSTED_MARKER_VALUE,  # names/titles are instructor-controlled text
        "course_id": course_id,
        "course": course["label"],
        "kind": kind,
        "change": change,
        "id": item_id,
        "name": name,
    }
    if detail:
        row["detail"] = detail
    return row


def _describe(kind: str, old: dict, new: dict) -> str | None:
    """What differs for an item present in both snapshots, or None if nothing meaningful."""
    notes = []
    if kind == "assignment":
        if old["due_at"] != new["due_at"]:
            notes.append(f"due date {old['due_at'] or 'none'} → {new['due_at'] or 'none'}")
        if old["hidden"] != new["hidden"]:
            notes.append("now hidden" if new["hidden"] else "now visible")
    elif kind == "quiz":
        for field, label in (("due_at", "due date"), ("start_at", "opens"), ("end_at", "closes")):
            if old[field] != new[field]:
                notes.append(f"{label} {old[field] or 'none'} → {new[field] or 'none'}")
    elif kind == "announcement":
        if old["updated_at"] != new["updated_at"] or old["title"] != new["title"]:
            notes.append("edited")
    elif kind == "grade":
        if old["displayed"] != new["displayed"]:
            notes.append(f"{old['displayed'] or 'blank'} → {new['displayed'] or 'blank'}")
    if kind != "announcement" and old["name"] != new["name"]:
        notes.append(f"renamed from {old['name']!r}")
    return "; ".join(notes) or None


def diff_snapshots(old: dict, new: dict, failed_course_ids: set[str]) -> list[dict]:
    changes: list[dict] = []
    old_courses, new_courses = old.get("courses", {}), new.get("courses", {})
    for cid, course in new_courses.items():
        if cid in failed_course_ids:
            continue  # carried-forward data: nothing real to compare
        before = old_courses.get(cid)
        if before is None:
            changes.append(_change(cid, course, "course", "added", cid, course["name"]))
            continue
        for section, kind, name_key in _KINDS:
            o, n = before.get(section, {}), course.get(section, {})
            for item_id, item in n.items():
                if item_id not in o:
                    changes.append(_change(cid, course, kind, "added", item_id, item[name_key]))
                    continue
                detail = _describe(kind, o[item_id], item)
                if detail:
                    changes.append(_change(cid, course, kind, "changed", item_id, item[name_key], detail))
            for item_id, item in o.items():
                if item_id not in n:
                    changes.append(_change(cid, before, kind, "removed", item_id, item[name_key]))
    for cid, course in old_courses.items():
        if cid not in new_courses and cid not in failed_course_ids:
            changes.append(_change(cid, course, "course", "removed", cid, course["name"]))
    return changes


def check_for_changes(
    client: LearnClient,
    courses: list[Course],
    store: SnapshotStore,
    now: datetime | None = None,
    *,
    save: bool = True,
) -> dict[str, Any]:
    """Compare LEARN now against the saved snapshot, then (by default) advance the baseline."""
    now = now or datetime.now(timezone.utc)
    previous = store.load()
    new, errors = take_snapshot(client, courses, previous, now)
    failed = {e["course_id"] for e in errors}
    checked = [{"course_id": cid, "course_name": c["name"]} for cid, c in new["courses"].items() if cid not in failed]

    result: dict[str, Any] = {
        "first_run": previous is None,
        "since": previous["taken_at"] if previous else None,
        "checked_at": now.isoformat(),
        "courses_checked": checked,
        "changes": [] if previous is None else diff_snapshots(previous, new, failed),
        "errors": errors,
    }
    if previous is None:
        result["note"] = "No earlier snapshot existed, so this run saved a baseline; changes will show from the next check."
    if errors:
        result["note_errors"] = "Courses in 'errors' could not be checked this time; their changes are NOT reflected."
    if save:
        store.save(new)
    return result


def render(result: dict[str, Any]) -> str:
    lines: list[str] = []
    if result["first_run"]:
        lines.append(result["note"])
    else:
        lines.append(f"Changes since {result['since']}:")
        if not result["changes"]:
            lines.append("  nothing changed")
        for c in result["changes"]:
            detail = f" — {c['detail']}" if c.get("detail") else ""
            lines.append(f"  • [{c['course']}] {c['kind']} {c['change']}: {c['name']}{detail}")
    for e in result["errors"]:
        lines.append(f"⚠ Could not check {e['course_name']}: {e['message']}")
    return "\n".join(lines)
