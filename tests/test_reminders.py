import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from agent import changes, proactive, replies, reminders
from agent.ledger import Ledger
from agent.reminders import ReminderError, ReminderStore
from agent.tools import build_tools
from agent.watchers import TZ
from learn.exceptions import LearnAuthenticationError
from tests.test_changes import FakeClient, assignment

NOW = datetime(2026, 10, 10, 22, 42, tzinfo=TZ).astimezone(timezone.utc)  # Sat Oct 10, 10:42 PM Waterloo


def L(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def store(tmp_path):
    return ReminderStore(tmp_path / "reminders.json", clock=lambda: NOW)


@pytest.fixture
def tools(store):
    return reminders.build_tools(store, clock=lambda: NOW)


# ---- time resolution -------------------------------------------------------------------------


def test_at_is_interpreted_as_waterloo_local_time():
    due = reminders.resolve_time("2026-11-01T09:00", None, NOW)
    assert due == L(11, 1, 9, 0).astimezone(timezone.utc)  # also correct across the Nov 1 DST change


def test_in_minutes():
    assert reminders.resolve_time(None, 20, NOW) == NOW + timedelta(minutes=20)


@pytest.mark.parametrize(
    "at,in_minutes,fragment",
    [
        (None, None, "exactly one"),
        ("2026-11-01T09:00", 5, "exactly one"),
        ("2026-11-01", None, "date AND a time"),
        ("tomorrow at 9", None, "not a valid"),
        ("2026-13-45T99:00", None, "not a valid"),
        ("2026-10-10T22:00", None, "already past"),
        ("2027-12-01T09:00", None, "a year"),
        (None, 0, "at least 1"),
        (None, -5, "at least 1"),
        (None, True, "at least 1"),
        (None, "20", "at least 1"),
    ],
)
def test_bad_times_are_rejected_with_a_message_the_model_can_act_on(at, in_minutes, fragment):
    with pytest.raises(ReminderError, match=fragment):
        reminders.resolve_time(at, in_minutes, NOW)


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(minutes=20), "in 20 minutes"),
        (timedelta(minutes=1), "in 1 minute"),
        (timedelta(minutes=75), "in 1 hour 15 minutes"),
        (timedelta(hours=2), "in 2 hours"),
        (timedelta(hours=5, minutes=30), "in 5 hours 30 minutes"),
        (timedelta(days=1, hours=1), "tomorrow"),
        (timedelta(days=22), "in 22 days"),
    ],
)
def test_humanize(delta, expected):
    assert reminders.humanize_until(NOW + delta, NOW) == expected


# ---- tools ---------------------------------------------------------------------------------


def test_set_reminder_returns_a_computed_readback(tools):
    result = tools["set_reminder"]("email my TA", at="2026-11-01T09:00")
    assert result["ok"] and result["reminder_id"] == 1
    assert result["readback"] == "Reminder #1 set for Sun Nov 1, 9:00 AM (in 22 days): email my TA"


def test_set_reminder_in_minutes(tools):
    assert "(in 20 minutes)" in tools["set_reminder"]("stretch", in_minutes=20)["readback"]


def test_errors_say_nothing_was_saved_and_include_the_current_time(tools, store):
    err = tools["set_reminder"]("x", at="2026-10-09T09:00")
    assert err["error"] and err["type"] == "invalid_reminder" and "already past" in err["message"]
    assert err["now_local"] == "Sat Oct 10, 10:42 PM"
    assert store.active() == []


def test_text_is_cleaned_and_bounded(tools, store):
    assert tools["set_reminder"]("call\n`mom`   now", in_minutes=5)["readback"].endswith(": call 'mom' now")
    assert tools["set_reminder"]("x" * 201, in_minutes=5)["error"]
    assert tools["set_reminder"]("   ", in_minutes=5)["error"]
    assert tools["set_reminder"](None, in_minutes=5)["error"]
    assert len(store.active()) == 1


def test_pending_reminders_are_capped(tools):
    for i in range(reminders.MAX_ACTIVE):
        assert tools["set_reminder"](f"r{i}", in_minutes=10 + i)["ok"]
    over = tools["set_reminder"]("one too many", in_minutes=99)
    assert over["error"] and "too many" in over["message"]


def test_list_and_cancel(tools):
    tools["set_reminder"]("later", at="2026-11-01T09:00")
    tools["set_reminder"]("soon", in_minutes=30)
    listing = tools["list_reminders"]()
    assert [r["text"] for r in listing["reminders"]] == ["soon", "later"]  # soonest first
    assert tools["cancel_reminder"](2)["ok"]
    assert tools["cancel_reminder"](2)["type"] == "not_found"
    assert tools["cancel_reminder"]("two")["error"]
    assert [r["text"] for r in tools["list_reminders"]()["reminders"]] == ["later"]


def test_tools_are_registered_only_with_a_store(store):
    without = build_tools(object())
    assert not {"set_reminder", "list_reminders", "cancel_reminder"} & set(without)
    with_store = build_tools(object(), reminder_store=store)
    assert {"set_reminder", "list_reminders", "cancel_reminder"} <= set(with_store)


# ---- store ---------------------------------------------------------------------------------


def test_store_persists_ids_and_survives_corruption(tmp_path):
    path = tmp_path / "r.json"
    a = ReminderStore(path, clock=lambda: NOW)
    a.add("one", NOW + timedelta(hours=1))
    a.add("two", NOW + timedelta(hours=2))
    a.cancel(2)
    b = ReminderStore(path, clock=lambda: NOW)
    assert [r.text for r in b.active()] == ["one"]
    assert b.add("three", NOW + timedelta(hours=3)).id == 3  # ids are never reused
    path.write_text("{broken")
    assert ReminderStore(path).active() == []


def test_sent_reminders_are_pruned_after_a_week(store):
    r = store.add("done thing", NOW + timedelta(minutes=1))
    store.mark_sent([r.id], NOW)
    assert store.active() == []
    store.add("another", NOW + timedelta(minutes=2))
    store.mark_sent([2], NOW + timedelta(days=8))
    assert [r.id for r in store._items] == [2]  # #1 (sent 8 days ago) is gone; #2 was just sent


# ---- commands -------------------------------------------------------------------------------


def test_plain_commands_list_and_cancel(store, tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    store.add("call mom", NOW + timedelta(hours=2))
    out, _ = replies.handle("reminders", ledger, NOW, reminders=store)
    assert "#1" in out and "call mom" in out and "in 2 hours" in out
    assert "Cancelled reminder #1" in replies.handle("cancel #1", ledger, NOW, reminders=store)[0]
    assert "No pending reminder #1" in replies.handle("cancel 1", ledger, NOW, reminders=store)[0]
    assert replies.handle("reminders", ledger, NOW, reminders=store)[0] == "No pending reminders."
    assert replies.handle("reminders", ledger, NOW)[0] is None  # no store: not a command


# ---- delivery by the proactive loop ----------------------------------------------------------


class Notifier:
    def __init__(self):
        self.sent: list[str] = []
        self.fail = False

    async def send(self, text):
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append(text)
        return [len(self.sent)]


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def build(tmp_path, now, client=None):
    clock = Clock(now)
    store = ReminderStore(tmp_path / "reminders.json", clock=clock)
    notifier = Notifier()
    agent = proactive.ProactiveAgent(
        client or FakeClient({"1": {"a": [assignment("a1")]}}),
        notifier,
        ThreadPoolExecutor(max_workers=1),
        ledger=Ledger(tmp_path / "ledger.json"),
        store=changes.SnapshotStore(tmp_path / "snap.json"),
        clock=clock,
        reminders=store,
    )
    return agent, store, notifier, clock


def tick(agent, poll=False):
    return asyncio.run(agent.tick(poll=poll))


def test_reminder_fires_at_its_time_not_before(tmp_path):
    agent, store, notifier, clock = build(tmp_path, L(10, 11, 8, 0))
    store.add("start the report", L(10, 11, 9, 0))
    clock.now = L(10, 11, 8, 59)
    tick(agent)
    assert notifier.sent == []
    clock.now = L(10, 11, 9, 0)
    tick(agent)
    assert notifier.sent == ["⏰ Reminder: start the report"]
    tick(agent)
    assert len(notifier.sent) == 1  # never twice


def test_reminders_bypass_quiet_hours_but_not_mute(tmp_path):
    agent, store, notifier, clock = build(tmp_path, L(10, 11, 1, 0))
    store.add("up early", L(10, 11, 2, 0))
    clock.now = L(10, 11, 2, 0)  # 2 AM is inside quiet hours
    tick(agent)
    assert notifier.sent == ["⏰ Reminder: up early"]

    store.add("muted one", L(10, 11, 3, 0))
    agent.handle_command("mute 2h")
    clock.now = L(10, 11, 3, 0)
    tick(agent)
    assert len(notifier.sent) == 1
    clock.now = L(10, 11, 4, 30)  # mute over: it arrives late, and says so
    tick(agent)
    assert notifier.sent[-1] == "⏰ Reminder: muted one (was due Sun Oct 11, 3:00 AM)"


def test_several_due_reminders_are_batched(tmp_path):
    agent, store, notifier, clock = build(tmp_path, L(10, 11, 8, 0))
    store.add("a", L(10, 11, 8, 30))
    store.add("b", L(10, 11, 8, 31))
    clock.now = L(10, 11, 8, 32)
    tick(agent)
    assert notifier.sent == ["⏰ Reminders:\n• a\n• b"] or notifier.sent[0].startswith("⏰ Reminders:\n• a")
    assert len(notifier.sent) == 1


def test_failed_send_is_retried_and_survives_a_restart(tmp_path):
    agent, store, notifier, clock = build(tmp_path, L(10, 11, 8, 0))
    store.add("important", L(10, 11, 8, 5))
    clock.now = L(10, 11, 8, 6)
    notifier.fail = True
    tick(agent)
    assert [r.text for r in store.active()] == ["important"]  # not marked sent
    agent2, store2, notifier2, clock2 = build(tmp_path, L(10, 11, 8, 10))  # "restart"
    tick(agent2)
    assert notifier2.sent == ["⏰ Reminder: important (was due Sun Oct 11, 8:05 AM)"] or "important" in notifier2.sent[0]
    assert store2.active() == []


def test_reminders_fire_while_the_learn_session_is_expired(tmp_path):
    agent, store, notifier, clock = build(tmp_path, L(10, 11, 8, 0), client=FakeClient({"1": LearnAuthenticationError("expired", status=401)}))
    store.add("still works", L(10, 11, 8, 5))
    clock.now = L(10, 11, 8, 5)
    tick(agent, poll=True)
    assert notifier.sent[0] == "⏰ Reminder: still works"
    assert proactive.SIGNIN_NEEDED in notifier.sent  # and the sign-in notice still goes out


def test_loop_wakes_for_a_reminder_added_from_another_thread(tmp_path):
    """End to end with the real clock: the loop is asleep for ~15 min, a tool thread adds a
    reminder due in 1.5 s, and it must be delivered promptly (not at the next poll)."""

    async def scenario():
        real = lambda: datetime.now(timezone.utc)  # noqa: E731
        store = ReminderStore(tmp_path / "reminders.json", clock=real)
        notifier = Notifier()
        agent = proactive.ProactiveAgent(
            FakeClient({"1": {"a": []}}), notifier, ThreadPoolExecutor(max_workers=1),
            ledger=Ledger(tmp_path / "ledger.json"), store=changes.SnapshotStore(tmp_path / "snap.json"),
            clock=real, reminders=store,
        )
        task = asyncio.create_task(agent.run())
        await asyncio.sleep(0.5)  # first poll done; loop now sleeping until the next poll
        threading.Thread(target=lambda: store.add("ping me", real() + timedelta(seconds=1.5))).start()
        for _ in range(60):
            await asyncio.sleep(0.1)
            if notifier.sent:
                break
        task.cancel()
        return notifier.sent

    sent = asyncio.run(scenario())
    assert sent and sent[0].startswith("⏰ Reminder: ping me")


# ---- the readback guarantee and the time hint ------------------------------------------------


def test_readback_is_appended_when_the_model_paraphrases(tools, store):
    tools["set_reminder"]("call mom", in_minutes=60)
    reply = store.ensure_readbacks("OK. I've set a reminder to call mom in an hour (at 11:42 PM).")
    assert reply.endswith("✓ Reminder #1 set for Sat Oct 10, 11:42 PM (in 1 hour): call mom")
    assert store.ensure_readbacks("later turn") == "later turn"  # delivered once


def test_readback_is_not_duplicated_when_the_model_repeats_it(tools, store):
    line = tools["set_reminder"]("stretch", in_minutes=20)["readback"]
    assert store.ensure_readbacks(f"Done! {line}") == f"Done! {line}"


def test_failed_calls_add_no_readback(tools, store):
    tools["set_reminder"]("x", at="2020-01-01T09:00")
    assert store.ensure_readbacks("Sorry, that's in the past.") == "Sorry, that's in the past."


def test_session_adds_current_time_and_finalizes_reply():
    from discord_adapter.session import AgentSession

    seen = {}

    def fake_turn(llm, model, messages, tools, debug, usage):
        seen["user"] = messages[-1]["content"]
        return "ok"

    class Backend:
        name, model, unavailable_error = "fake", "m", RuntimeError

    session = AgentSession(
        None, Backend(), {}, ThreadPoolExecutor(1), run_turn_fn=fake_turn, time_hint=True,
        finalize_reply=lambda r: r + " [final]", clock=lambda: NOW,
    )
    reply = asyncio.run(session.ask("remind me tomorrow at 9"))
    assert seen["user"] == "[Current time: Saturday, October 10, 2026, 10:42 PM, Waterloo time]\nremind me tomorrow at 9"
    assert reply == "ok [final]"
