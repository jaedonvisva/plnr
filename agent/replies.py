"""Deterministic handling of the few things you can say back to a proactive message:

    done / done 2 / done 1 3 / done all / submitted      mark items handed in (stops reminders)
    undo                                                 un-mark the last "done"
    mute 2h / mute 30m / stop tonight / unmute           silence proactive messages
    reminders / cancel 3                                 list / cancel your reminders (the chat
                                                         agent sets them: "remind me ...")
    !digest                                              the morning summary, on demand

Anything else is not a command (returns None) and goes to the normal chat agent. No model
is involved here, so a reply can never be misread by one.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import reminders as reminders_module
from .ledger import Ledger
from .watchers import DEFAULT_SCHEDULE, TZ, Schedule, next_morning

_DONE = re.compile(r"^(?:i'?m |i am )?(?:done|submitted|handed in|finished)(?: with)?(?: (?P<which>.+?))?[.!]*$")
_MUTE = re.compile(r"^(?:mute|quiet|snooze)(?: for)? (?P<n>\d{1,3})\s*(?P<unit>m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)[.!]*$")
_CANCEL = re.compile(r"^cancel(?: reminder)? #?(?P<id>\d{1,6})[.!]*$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def _numbers(spec: str | None) -> list[int] | str | None:
    """None -> no number given; 'all'; or a list of ints. Returns '?' for unparseable text."""
    if spec is None:
        return None
    spec = spec.strip().lower()
    if spec == "all":
        return "all"
    parts = [p for p in re.split(r"[\s,]+|and", spec) if p]
    if parts and all(p.isdigit() for p in parts):
        return [int(p) for p in parts]
    return "?"


def _clock(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%I:%M %p").lstrip("0")


def handle(
    text: str,
    ledger: Ledger,
    now: datetime,
    reply_to_id: int | None = None,
    schedule: Schedule = DEFAULT_SCHEDULE,
    reminders=None,
) -> tuple[str | None, bool]:
    """Returns (reply, wants_digest). reply is None when `text` is not a command."""
    t = " ".join(text.lower().split())

    if t == "!digest":
        return None, True

    if reminders is not None:
        if t in ("reminders", "my reminders", "list reminders"):
            return reminders_module.render_list(reminders, now), False
        cancel = _CANCEL.match(t)
        if cancel:
            removed = reminders.cancel(int(cancel["id"]))
            return (f"Cancelled reminder #{removed.id}: {removed.text}" if removed else f"No pending reminder #{cancel['id']}."), False

    if t == "undo":
        label = ledger.undo_last_done()
        return (f"Back on the list: {label}." if label else "Nothing to undo."), False

    if t == "unmute":
        ledger.mute_until(None)
        return "Unmuted.", False

    if t in ("stop tonight", "mute tonight", "quiet tonight"):
        until = next_morning(now, schedule)
        ledger.mute_until(until)
        return f"Okay, quiet until {_clock(until)} tomorrow." if until.date() != now.astimezone(TZ).date() else f"Okay, quiet until {_clock(until)}.", False

    m = _MUTE.match(t)
    if m:
        unit = _UNITS[m["unit"][0]]
        until = now + timedelta(**{unit: int(m["n"])})
        ledger.mute_until(until)
        return f"Muted until {_clock(until)}.", False

    m = _DONE.match(t)
    # "done reading the syllabus, what next?" is a question for the chat agent, not a command.
    if m and _numbers(m["which"]) != "?":
        return _mark_done(m["which"], ledger, now, reply_to_id), False

    return None, False


def _mark_done(which: str | None, ledger: Ledger, now: datetime, reply_to_id: int | None) -> str:
    items = ledger.items_for(reply_to_id) or ledger.items_for(None)
    if not items:
        return "I don't have a recent reminder to match that to."
    picked = _numbers(which)
    if picked == "?":
        return f'I didn\'t get which item. Say "done 1" (1–{len(items)}) or "done all".'
    if picked is None:
        if len(items) > 1:
            listing = "\n".join(f"{i}. {it['label']}" for i, it in enumerate(items, 1))
            return f'Which one?\n{listing}\nSay "done 1", "done 1 2" or "done all".'
        chosen = items
    elif picked == "all":
        chosen = items
    else:
        bad = [n for n in picked if not 1 <= n <= len(items)]
        if bad:
            return f"There's no item {bad[0]} in that message (it has 1–{len(items)})."
        chosen = [items[n - 1] for n in dict.fromkeys(picked)]
    for it in chosen:
        ledger.mark_done(it["uid"], it["label"], now)
    names = "; ".join(it["label"] for it in chosen)
    return f"Marked done: {names}. No more reminders for {'it' if len(chosen) == 1 else 'those'}."
