"""Deterministic watchers for the always-on agent: pure functions that turn LEARN state
(a snapshot from agent/changes.py) plus the clock into `Event`s, and `compose` that batches
events into one chat message.

No model is involved in deciding what to send or what it says. Instructor-written text
(item names, announcement titles) is only ever quoted after `clean()`.

Timing is anchored to the student's day, not to "N hours before", because deadlines here are
usually 11:59 PM:
  - 8:00 digest: past due / today / tomorrow / this week
  - 18:00 "due tonight" and 22:30 "last call" for items due late in the evening
  - items due before noon are announced the evening before (their 18:00/22:30 slots)
  - items due between noon and 8 PM get one "soon" nudge 3 hours before
  - nothing is sent from 23:30 to 08:00; reminders simply wait (and are dropped if the
    deadline passes first), changes surface at the next daytime cycle
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from .aggregation import WATERLOO_TZ
from .today import DEFAULT_OVERDUE_DAYS, DEFAULT_UPCOMING_DAYS, Deadline

TZ = WATERLOO_TZ
MAX_TEXT = 100


@dataclass(frozen=True)
class Schedule:
    digest_at: time = time(8, 0)
    digest_until: time = time(12, 0)
    tonight_at: time = time(18, 0)
    last_call_at: time = time(22, 30)
    quiet_start: time = time(23, 30)
    quiet_end: time = time(8, 0)
    early_due_before: time = time(12, 0)  # due earlier than this -> announce the evening before
    late_due_from: time = time(20, 0)  # due at/after this -> tonight + last call the same day
    soon_lead: timedelta = timedelta(hours=3)
    last_call_margin: timedelta = timedelta(minutes=20)


DEFAULT_SCHEDULE = Schedule()


# ---- model ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    uid: str
    label: str


@dataclass(frozen=True)
class Entry:
    text: str
    item: Item | None = None


@dataclass(frozen=True)
class Section:
    heading: str
    entries: tuple[Entry, ...] = ()


@dataclass(frozen=True)
class Event:
    keys: tuple[str, ...]  # all marked sent together once the message is delivered
    kind: str  # "digest" | "reminder" | "change"
    sections: tuple[Section, ...]
    intro: str = ""


@dataclass(frozen=True)
class Message:
    text: str
    items: tuple[Item, ...] = ()
    keys: tuple[str, ...] = field(default_factory=tuple)


# ---- small helpers ------------------------------------------------------------------------


def clean(text: str | None) -> str:
    """Instructor-controlled text -> one short, markdown-quiet line."""
    flat = " ".join((text or "").split()).replace("`", "'")
    return flat if len(flat) <= MAX_TEXT else flat[: MAX_TEXT - 1].rstrip() + "…"


def _clock(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%I:%M %p").lstrip("0")


def _day_clock(dt: datetime) -> str:
    local = dt.astimezone(TZ)
    return f"{local.strftime('%a %b')} {local.day}, {_clock(dt)}"


def _at(day, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=TZ)


def is_quiet(now: datetime, schedule: Schedule = DEFAULT_SCHEDULE) -> bool:
    t = now.astimezone(TZ).time()
    return t >= schedule.quiet_start or t < schedule.quiet_end


def next_morning(now: datetime, schedule: Schedule = DEFAULT_SCHEDULE) -> datetime:
    """The next time quiet hours end (digest time), strictly after `now`."""
    local = now.astimezone(TZ)
    candidate = _at(local.date(), schedule.quiet_end)
    return candidate if candidate > local else _at(local.date() + timedelta(days=1), schedule.quiet_end)


def _entry(d: Deadline, with_day: bool = False) -> Entry:
    when = _day_clock(d.due_at) if with_day else _clock(d.due_at)
    text = f"{d.course_label}: {clean(d.name)} — {when}"
    return Entry(text, Item(d.uid_source, f"{d.course_label} {clean(d.name)}"))


# ---- deadlines from a snapshot ------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def deadlines_from_snapshot(snapshot: dict) -> list[Deadline]:
    """Dated, visible assignments and quizzes from every course in the snapshot."""
    out: list[Deadline] = []
    for cid, course in snapshot.get("courses", {}).items():
        for aid, a in course.get("assignments", {}).items():
            due = _parse_dt(a.get("due_at"))
            if due is None or a.get("hidden"):
                continue
            out.append(
                Deadline(
                    f"assignment:{cid}:{aid}", cid, course["name"], course["label"], "assignment", aid,
                    a["name"], due, bool(a.get("graded")),
                )
            )
        for qid, q in course.get("quizzes", {}).items():
            due = _parse_dt(q.get("due_at"))
            if due is None:
                continue
            out.append(
                Deadline(f"quiz:{cid}:{qid}", cid, course["name"], course["label"], "quiz", qid, q["name"], due)
            )
    out.sort(key=lambda d: (d.due_at, d.course_label, d.name))
    return out


# ---- reminders ----------------------------------------------------------------------------


def reminder_slots(d: Deadline, schedule: Schedule = DEFAULT_SCHEDULE) -> list[tuple[str, datetime]]:
    """When this deadline should be mentioned, oldest first, as (slot name, time)."""
    due_local = d.due_at.astimezone(TZ)
    due_time = due_local.time()
    if due_time >= schedule.late_due_from:
        day = due_local.date()
    elif due_time < schedule.early_due_before:
        day = due_local.date() - timedelta(days=1)  # evening before
    else:
        soon = max(_at(due_local.date(), schedule.digest_at) + timedelta(hours=1), d.due_at - schedule.soon_lead)
        return [("soon", soon)]
    slots = [("tonight", _at(day, schedule.tonight_at))]
    last = _at(day, schedule.last_call_at)
    if last < d.due_at - schedule.last_call_margin:
        slots.append(("last", last))
    return slots


def reminder_events(
    deadlines: list[Deadline], now: datetime, is_sent, is_done, schedule: Schedule = DEFAULT_SCHEDULE
) -> list[Event]:
    """At most one event per deadline: the latest slot reached. Earlier reached slots are
    marked sent with it, so waking from sleep never replays a backlog."""
    today = now.astimezone(TZ).date()
    events: list[Event] = []
    for d in deadlines:
        if d.due_at <= now or d.graded or is_done(d.uid_source):
            continue
        reached = [(name, t) for name, t in reminder_slots(d, schedule) if t <= now]
        if not reached:
            continue
        keys = [f"remind:{d.uid_source}:{name}:{d.due_at.isoformat()}" for name, _ in reached]
        if is_sent(keys[-1]):
            continue
        name = reached[-1][0]
        same_day = d.due_at.astimezone(TZ).date() == today
        if name == "tonight":
            heading = "Due tonight" if same_day else "Due tomorrow morning"
        elif name == "last":
            heading = "Last call — due tonight" if same_day else "Last call — due tomorrow morning"
        else:
            heading = "Due soon"
        events.append(Event(tuple(keys), "reminder", (Section(heading, (_entry(d),)),)))
    return events


# ---- digest -------------------------------------------------------------------------------


def digest_event(
    deadlines: list[Deadline],
    now: datetime,
    is_sent,
    is_done,
    schedule: Schedule = DEFAULT_SCHEDULE,
    *,
    force: bool = False,
) -> Event | None:
    local = now.astimezone(TZ)
    key = f"digest:{local.date().isoformat()}"
    if not force:
        if not (schedule.digest_at <= local.time() < schedule.digest_until) or is_sent(key):
            return None
    today = local.date()
    tomorrow = today + timedelta(days=1)
    horizon = now + timedelta(days=DEFAULT_UPCOMING_DAYS)
    past, due_today, due_tomorrow, later = [], [], [], []
    for d in deadlines:
        if is_done(d.uid_source):
            continue
        if d.due_at < now:
            if d.kind == "assignment" and not d.graded and d.due_at >= now - timedelta(days=DEFAULT_OVERDUE_DAYS):
                past.append(d)
            continue
        day = d.due_at.astimezone(TZ).date()
        if day == today:
            due_today.append(d)
        elif day == tomorrow:
            due_tomorrow.append(d)
        elif d.due_at <= horizon:
            later.append(d)
    sections = []
    if past:
        sections.append(Section("Past due, not graded yet (LEARN doesn't show if you submitted)", tuple(_entry(d, True) for d in past)))
    if due_today:
        sections.append(Section("Due today", tuple(_entry(d) for d in due_today)))
    if due_tomorrow:
        sections.append(Section("Due tomorrow", tuple(_entry(d) for d in due_tomorrow)))
    if later:
        sections.append(Section("This week", tuple(_entry(d, True) for d in later)))
    if not sections:
        if not force:
            return Event((key,), "digest", ())  # mark sent; nothing worth a message
        sections.append(Section("Nothing due in the next 7 days"))
    return Event((key,), "digest", tuple(sections), intro=f"Good morning — {local.strftime('%A, %B')} {local.day}.")


# ---- changes ------------------------------------------------------------------------------


def _change_key(old: dict, c: dict, new_item: dict | None) -> str:
    raw = "|".join(
        [
            old.get("taken_at", ""), c["course_id"], c["kind"], c["id"], c["change"], c.get("detail") or "",
            (new_item or {}).get("updated_at") or "",
        ]
    )
    return "chg:" + hashlib.sha1(raw.encode()).hexdigest()[:16]


_SECTION_KIND = {"announcement": "announcements", "grade": "grades", "assignment": "assignments", "quiz": "quizzes"}


def change_events(old: dict, new: dict, changes: list[dict]) -> list[Event]:
    """Student-relevant subset of agent/changes.py's diff: new/edited announcements, posted
    grades, new items, and moved dates. Removals and course add/remove are not pushed."""
    events: list[Event] = []
    for c in changes:
        section = _SECTION_KIND.get(c["kind"])
        if section is None or c["change"] == "removed":
            continue
        item = new.get("courses", {}).get(c["course_id"], {}).get(section, {}).get(c["id"], {})
        name, where = clean(c["name"]), c["course"]
        if c["kind"] == "announcement":
            text = f"New announcement in {where}: {name}" if c["change"] == "added" else f"Announcement edited in {where}: {name}"
        elif c["kind"] == "grade":
            shown = item.get("displayed")
            if not shown:
                continue
            text = f"Grade posted in {where}: {name} — {shown}"
        else:
            if item.get("hidden"):
                continue
            if c["change"] == "added":
                due = _parse_dt(item.get("due_at"))
                text = f"New {c['kind']} in {where}: {name}" + (f" — due {_day_clock(due)}" if due else "")
            else:
                text = f"{c['kind'].capitalize()} updated in {where}: {name} ({clean(c.get('detail'))})"
        events.append(Event((_change_key(old, c, item),), "change", (Section("Heads up", (Entry(text),)),)))
    return events


# ---- batching -----------------------------------------------------------------------------


def compose(events: list[Event]) -> Message | None:
    """One chat message for all events. Sections with the same heading merge; an item listed
    once is not listed again; entries with items are numbered for "done N"."""
    if not events:
        return None
    ordered = sorted(events, key=lambda e: {"change": 0, "digest": 1, "reminder": 2}[e.kind])
    keys = tuple(k for e in ordered for k in e.keys)

    merged: dict[str, list[Entry]] = {}
    for event in ordered:
        for section in event.sections:
            merged.setdefault(section.heading, []).extend(section.entries)

    seen: set[str] = set()
    items: list[Item] = []
    blocks: list[str] = []
    for heading, entries in merged.items():
        lines = []
        for entry in entries:
            if entry.item is not None:
                if entry.item.uid in seen:
                    continue
                seen.add(entry.item.uid)
                items.append(entry.item)
                lines.append(f"{len(items)}. {entry.text}")
            else:
                lines.append(f"• {entry.text}")
        if lines or not entries:
            blocks.append("\n".join([f"**{heading}**"] + lines))
    intro = next((e.intro for e in ordered if e.intro), "")
    if not blocks:
        return Message("", (), keys)  # everything was already covered; still counts as sent
    parts = ([intro] if intro else []) + blocks
    if items:
        parts.append('Reply "done 1" once something is handed in.' if len(items) > 1 else 'Reply "done" once it\'s handed in.')
    return Message("\n\n".join(parts), tuple(items), keys)
