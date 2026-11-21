"""The always-on loop: poll LEARN, decide deterministically what is worth saying, send it as
one chat message, remember that it was sent.

    plan_cycle()   synchronous and side-effect free apart from reading LEARN: fetch ->
                   snapshot -> watchers -> a Plan (message + the state to commit on success)
    commit()       mark keys sent, advance the baseline snapshot -- only after delivery
    ProactiveAgent the asyncio loop around those, plus sign-in-expired handling

Delivery is at-least-once: state is committed only after the message was sent, so a failed
send is retried next cycle and a crash between send and commit can repeat one message.

The loop never opens a browser. An expired session means "tell the user once, then wait for
`python agent.py auth` to produce a fresh one" (see `refresh_session`).
"""

from __future__ import annotations

import asyncio
import random
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from learn.exceptions import LearnApiError, LearnAuthenticationError

from . import changes, replies, reminders as reminders_module, watchers
from .changes import SnapshotStore
from .ledger import Ledger
from .watchers import Message, Schedule

POLL_SECONDS = 15 * 60
QUIET_POLL_MULTIPLIER = 3
LATE_REMINDER_AFTER = timedelta(minutes=5)
"""A reminder delivered this much after its time is labelled with when it was due."""
SIGNIN_RETRY_SECONDS = 5 * 60
ERROR_RETRY_SECONDS = 5 * 60
JITTER = 0.1

SIGNIN_NEEDED = (
    "LEARN sign-in expired, so I can't check for changes right now. "
    "Run `python agent.py auth` where you have a browser (add `auth --push user@server` if I run "
    "on a server) and I'll pick it back up automatically."
)
SIGNIN_RESTORED = "Signed back in to LEARN — watching again."


class Notifier(Protocol):
    async def send(self, text: str) -> list[int]:
        """Deliver `text`; return the ids of the sent messages (for reply matching)."""


@dataclass
class Plan:
    message: Message | None = None  # None -> nothing to say
    keys: list[str] = field(default_factory=list)  # to mark sent (even when nothing is said)
    new_snapshot: dict | None = None  # baseline to advance to once delivered
    errors: list[dict[str, str]] = field(default_factory=list)
    suppressed: str | None = None  # "muted" | "quiet": fetched, but held back


def plan_cycle(client, store: SnapshotStore, ledger: Ledger, now: datetime, schedule: Schedule = watchers.DEFAULT_SCHEDULE) -> Plan:
    """Raises LearnAuthenticationError if the session expired."""
    courses = client.get_courses()
    previous = store.load()
    new, errors = changes.take_snapshot(client, courses, previous, now)
    failed = {e["course_id"] for e in errors}

    if ledger.is_muted(now):
        suppressed = "muted"
    elif watchers.is_quiet(now, schedule):
        suppressed = "quiet"
    else:
        suppressed = None

    # First ever run: save the baseline so changes show from the next cycle, even if held back.
    if suppressed:
        return Plan(new_snapshot=new if previous is None else None, errors=errors, suppressed=suppressed)

    deadlines = watchers.deadlines_from_snapshot(new)
    events: list[watchers.Event] = []
    if previous is not None:
        diff = changes.diff_snapshots(previous, new, failed)
        events += [e for e in watchers.change_events(previous, new, diff) if not ledger.was_sent(e.keys[-1])]
    digest = watchers.digest_event(deadlines, now, ledger.was_sent, ledger.is_done, schedule)
    if digest:
        events.append(digest)
    events += watchers.reminder_events(deadlines, now, ledger.was_sent, ledger.is_done, schedule)

    message = watchers.compose(events)
    keys = [k for e in events for k in e.keys]
    if message is not None and not message.text:
        message = None  # all covered elsewhere; just record the keys
    return Plan(message=message, keys=keys, new_snapshot=new, errors=errors)


def commit(plan: Plan, store: SnapshotStore, ledger: Ledger, now: datetime) -> None:
    if plan.keys:
        ledger.mark_sent(plan.keys, now)
    if plan.new_snapshot is not None:
        store.save(plan.new_snapshot)


class ProactiveAgent:
    def __init__(
        self,
        client,
        notifier: Notifier,
        executor,
        *,
        ledger: Ledger | None = None,
        store: SnapshotStore | None = None,
        schedule: Schedule = watchers.DEFAULT_SCHEDULE,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        reminders: reminders_module.ReminderStore | None = None,
        refresh_session: Callable[[], bool] | None = None,
        on_sent: Callable[[str], None] | None = None,
        poll_seconds: float = POLL_SECONDS,
    ):
        self._client = client
        self._notifier = notifier
        self._executor = executor
        self.ledger = ledger or Ledger()
        self._store = store or SnapshotStore(changes.PROJECT_ROOT / ".plnr" / "proactive_snapshot.json")
        self._schedule = schedule
        self._clock = clock
        self._reminders = reminders
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._next_poll_at: datetime | None = None
        if reminders is not None:
            reminders.on_change = self.notify_changed
        self._refresh_session = refresh_session
        self._on_sent = on_sent
        self._poll_seconds = poll_seconds

    # ---- loop -----------------------------------------------------------------------------

    def notify_changed(self) -> None:
        """Wake the loop so a newly added or cancelled reminder is scheduled right away.
        Safe to call from any thread (the reminder tools run on the worker thread)."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        while True:
            now = self._clock()
            poll = self._next_poll_at is None or now >= self._next_poll_at
            delay = await self.tick(poll=poll)
            if poll:
                self._next_poll_at = self._clock() + timedelta(seconds=delay * random.uniform(1 - JITTER, 1 + JITTER))
            # Clear BEFORE reading the schedule: a reminder added after this point sets the event
            # again, so the sleep below returns immediately instead of missing it.
            self._wake.clear()
            wait = (self._next_poll_at - self._clock()).total_seconds()
            until_reminder = self._seconds_until_reminder()
            if until_reminder is not None:
                wait = min(wait, until_reminder)
            await self._sleep_or_wake(max(1.0, wait))

    async def _sleep_or_wake(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _seconds_until_reminder(self) -> float | None:
        if self._reminders is None:
            return None
        due = self._reminders.next_due()
        if due is None:
            return None
        now = self._clock()
        muted_until = self.ledger.muted_until()
        if muted_until is not None and muted_until > now:
            due = max(due, muted_until)  # held until the mute ends; no point waking sooner
        return (due - now).total_seconds()

    async def tick(self, *, poll: bool = True) -> float:
        """One wake-up. Never raises. Always delivers due reminders; polls LEARN only if `poll`.
        Returns how long to wait before the next LEARN poll (0.0 when no poll was made)."""
        loop = asyncio.get_running_loop()
        await self._fire_reminders()
        if not poll:
            return 0.0
        try:
            return await self._tick(loop)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- the daemon must outlive any single bad cycle
            print("Proactive cycle failed:\n" + traceback.format_exc())
            return ERROR_RETRY_SECONDS

    async def _fire_reminders(self) -> None:
        """Send reminders that are due -- at their exact time, even in quiet hours (the user asked
        for that time), unless muted. Independent of LEARN, so they work while signed out."""
        if self._reminders is None:
            return
        try:
            now = self._clock()
            due = self._reminders.due(now)
            if not due or self.ledger.is_muted(now):
                return
            lines = []
            for r in due:
                late = f" (was due {reminders_module.fmt_local(r.due_at)})" if now - r.due_at > LATE_REMINDER_AFTER else ""
                lines.append(f"{r.text}{late}")
            text = f"⏰ Reminder: {lines[0]}" if len(lines) == 1 else "⏰ Reminders:\n" + "\n".join(f"• {line}" for line in lines)
            await self._deliver(text, [], now)
            self._reminders.mark_sent([r.id for r in due], now)  # only after it was delivered
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a failed send is retried on the next wake-up
            print("Reminder delivery failed:\n" + traceback.format_exc())

    async def _tick(self, loop) -> float:
        now = self._clock()
        if self.ledger.signin_notified and self._refresh_session is not None:
            if not await loop.run_in_executor(self._executor, self._refresh_session):
                return SIGNIN_RETRY_SECONDS
        try:
            plan = await loop.run_in_executor(
                self._executor, plan_cycle, self._client, self._store, self.ledger, now, self._schedule
            )
        except LearnAuthenticationError:
            if not self.ledger.signin_notified:
                await self._deliver(SIGNIN_NEEDED, [], now)
                self.ledger.set_signin_notified(True)
            return SIGNIN_RETRY_SECONDS
        except LearnApiError as exc:
            print(f"Proactive cycle: LEARN error: {exc}")
            return ERROR_RETRY_SECONDS

        for error in plan.errors:
            print(f"Proactive cycle: could not check {error['course_name']}: {error['message']}")

        if self.ledger.signin_notified:
            await self._deliver(SIGNIN_RESTORED, [], now)
            self.ledger.set_signin_notified(False)

        if plan.message is not None:
            await self._deliver(plan.message.text, plan.message.items, now)
        commit(plan, self._store, self.ledger, now)
        return self._poll_seconds * (QUIET_POLL_MULTIPLIER if plan.suppressed == "quiet" else 1)

    async def _deliver(self, text: str, items, now: datetime) -> None:
        ids = await self._notifier.send(text)
        listed = [{"uid": i.uid, "label": i.label} for i in items]
        for message_id in ids:
            self.ledger.remember_message(message_id, listed, now)
        if self._on_sent:
            self._on_sent(text)

    # ---- replies --------------------------------------------------------------------------

    def handle_command(self, text: str, reply_to_id: int | None = None) -> str | None:
        """Reply text if `text` is a proactive-agent command, else None (not ours)."""
        now = self._clock()
        reply, wants_digest = replies.handle(
            text, self.ledger, now, reply_to_id, self._schedule, reminders=self._reminders
        )
        if wants_digest:
            return self._digest_now(now)
        return reply

    def _digest_now(self, now: datetime) -> str:
        snapshot = self._store.load()
        if snapshot is None:
            return "I haven't checked LEARN yet — give me a few minutes."
        deadlines = watchers.deadlines_from_snapshot(snapshot)
        event = watchers.digest_event(deadlines, now, self.ledger.was_sent, self.ledger.is_done, self._schedule, force=True)
        message = watchers.compose([event]) if event else None
        if message is None:
            return "Nothing to show."
        # A digest shown on request also becomes the referent for "done N".
        self.ledger.remember_message(0, [{"uid": i.uid, "label": i.label} for i in message.items], now)
        return message.text
