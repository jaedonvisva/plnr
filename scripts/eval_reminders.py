"""Does the live chat model set reminders correctly?

Drives the real model (Gemini by default, or --model qwen) through agent.run_turn with the real
reminder tools and a FIXED clock (Saturday, Oct 10 2026, 10:42 PM Waterloo), then checks what ended
up in a throwaway store: right time, asked-instead-of-guessed, refused recurring, readback repeated.

    .venv/bin/python scripts/eval_reminders.py [--model qwen] [--verbose]

Costs a few cents of API usage. Unlike the unit tests this exercises the PROMPT, so rerun it after
changing agent/system_prompt.py or switching models.
"""

import importlib.util
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import backend as backend_module  # noqa: E402
from agent import reminders  # noqa: E402
from agent.loop import run_turn  # noqa: E402
from agent.reminders import ReminderStore  # noqa: E402
from agent.system_prompt import SYSTEM_PROMPT  # noqa: E402
from agent.watchers import TZ  # noqa: E402

_spec = importlib.util.spec_from_file_location("agent_cli", ROOT / "agent.py")
agent_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_cli)

NOW = datetime(2026, 10, 10, 22, 42, tzinfo=TZ).astimezone(timezone.utc)


def L(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def fake_get_upcoming_items(days_ahead: int = 14) -> dict:
    """Upcoming deadlines across all courses (assignments and quizzes with due dates)."""
    return {"items": [{"course": "MSE 232", "kind": "assignment", "name": "Assignment 4", "due_at": "2026-10-17T03:59:00+00:00", "due_local": "Fri Oct 16, 11:59 PM"}]}


# (message, expectation). expectation: ("at", datetime, keyword) | ("ask",) | ("none",)
CASES = [
    ("remind me in 20 minutes to stretch", ("at", NOW + timedelta(minutes=20), "stretch")),
    ("ping me in an hour to call mom", ("at", NOW + timedelta(hours=1), "mom")),
    ("remind me tomorrow at 9am to email my TA", ("at", L(10, 11, 9), "TA")),
    ("remind me tomorrow morning to review my notes", ("at", L(10, 11, 9), "notes")),
    ("remind me tonight at 11:30 to lock the door", ("at", L(10, 10, 23, 30), "door")),
    ("remind me on Friday at 3pm to submit the form", ("at", L(10, 16, 15), "form")),
    ("remind me on Monday at 8am to start the lab", ("at", L(10, 12, 8), "lab")),
    ("remind me the day after tomorrow at noon to pick up the package", ("at", L(10, 12, 12), "package")),
    ("remind me in 3 days at 6pm to water the plants", ("at", L(10, 13, 18), "plants")),
    ("remind me on November 3rd at 10am about the midterm", ("at", L(11, 3, 10), "midterm")),
    ("remind me tomorrow evening at 7:30 to go to the gym", ("at", L(10, 11, 19, 30), "gym")),
    ("remind me to email my TA", ("ask",)),
    ("remind me later to buy milk", ("ask",)),
    ("remind me every Sunday to do laundry", ("none",)),
    ("remind me yesterday to pay rent", ("none",)),
    ("remind me the day before my Assignment 4 is due to proofread it", ("day", (2026, 10, 15), "proofread")),
]


def main() -> int:
    args = sys.argv[1:]
    verbose = "--verbose" in args
    backend = backend_module.resolve(agent_cli._model_arg(args))
    client = agent_cli._connect(backend)
    print(f"Model: {backend.name} ({backend.model}); fixed now = Sat Oct 10 2026, 10:42 PM Waterloo\n")

    passed = outages = 0
    for message, expect in CASES:
        store = ReminderStore(Path(tempfile.mkdtemp()) / "r.json", clock=lambda: NOW)
        tools = reminders.build_tools(store, clock=lambda: NOW)
        tools["get_upcoming_items"] = fake_get_upcoming_items
        local = NOW.astimezone(TZ)
        stamp = f"[Current time: {local.strftime('%A, %B')} {local.day}, {local.year}, {local.strftime('%I:%M %p').lstrip('0')}, Waterloo time]"
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"{stamp}\n{message}"}]
        reply = None
        for attempt in range(1, 6):  # the backend's own 503 "high demand" is transient, not a prompt failure
            try:
                reply = run_turn(client, backend.model, messages[:2], tools, debug=False)
                break
            except backend.unavailable_error as exc:
                for r in store.active():  # a half-finished attempt must not leak into the next one
                    store.cancel(r.id)
                time.sleep(4 * attempt)
        if reply is None:
            outages += 1
            print(f"OUTAGE {message!r}: backend unavailable after retries (not counted as a failure)")
            continue
        verbatim = all(r_line in reply for r_line in store._readbacks)  # did the model repeat it itself?
        reply = store.ensure_readbacks(reply)  # what the user actually sees
        active = store.active()
        ok, why = False, ""
        if expect[0] == "at":
            _, want, kw = expect
            if len(active) != 1:
                why = f"{len(active)} reminders saved"
            else:
                r = active[0]
                off = abs((r.due_at - want).total_seconds()) / 60
                readback_ok = reminders.fmt_local(r.due_at) in reply and f"#{r.id}" in reply
                ok = off <= 1 and kw.lower() in r.text.lower() and readback_ok
                why = f"due {reminders.fmt_local(r.due_at)} (want {reminders.fmt_local(want)}), text={r.text!r}, readback_in_reply={readback_ok}, model_repeated_it={verbatim}"
        elif expect[0] == "day":
            _, ymd, kw = expect
            if len(active) != 1:
                why = f"{len(active)} reminders saved"
            else:
                r = active[0]
                local_due = r.due_at.astimezone(TZ)
                ok = (local_due.year, local_due.month, local_due.day) == ymd and kw in r.text.lower()
                why = f"due {reminders.fmt_local(r.due_at)} (want Oct 15 any time), text={r.text!r}"
        elif expect[0] == "ask":
            ok = not active and "?" in reply
            why = f"saved={len(active)}, asked={'?' in reply}"
        else:  # none
            ok = not active
            why = f"saved={len(active)}"
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {message}\n      {why}")
        if verbose or not ok:
            print(f"      reply: {reply[:220]!r}")
    ran = len(CASES) - outages
    print(f"\n{passed}/{ran} passed" + (f" ({outages} skipped: backend outage)" if outages else ""))
    return 0 if passed == ran else 1


if __name__ == "__main__":
    sys.exit(main())
