from datetime import datetime, timedelta, timezone

from agent import today
from learn.exceptions import LearnApiError
from learn.models import Assignment, Course, Quiz

# Wed Oct 14 2026, 10:00 Toronto (EDT, UTC-4) == 14:00 UTC
NOW = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)


def course(id_="1", name="Intro", display="MSE 232", active=True):
    return Course(id=id_, name=name, code=None, is_active=active, url="u", last_accessed_at=None, display_code=display)


def assignment(id_, due, name="HW", earned=None, hidden=False):
    return Assignment(
        id=id_, course_id="1", name=name, description=None, due_at=due, max_points=10, grade_item_id=None,
        points_earned=earned, points_possible=10 if earned is not None else None, is_hidden=hidden,
    )


def quiz(id_, due, name="Quiz"):
    return Quiz(id=id_, course_id="1", name=name, is_active=True, grade_item_id=None, description=None, due_at=due)


class FakeClient:
    def __init__(self, assignments=None, quizzes=None):
        self.a = assignments or {}
        self.q = quizzes or {}

    def get_assignments(self, cid):
        v = self.a.get(cid, [])
        if isinstance(v, Exception):
            raise v
        return v

    def get_quizzes(self, cid):
        v = self.q.get(cid, [])
        if isinstance(v, Exception):
            raise v
        return v


def deadlines(client, courses=None):
    return today.collect_deadlines(client, courses or [course()])


def test_collect_skips_undated_hidden_and_inactive_and_reports_errors():
    client = FakeClient(
        assignments={
            "1": [assignment("a", NOW + timedelta(days=1)), assignment("b", None), assignment("c", NOW, hidden=True)],
            "2": LearnApiError("boom"),
            "3": [assignment("z", NOW + timedelta(days=1))],
        },
        quizzes={"1": [quiz("q", NOW + timedelta(days=2)), quiz("q2", None)]},
    )
    ds, errors, active = today.collect_deadlines(
        client, [course("1"), course("2", "Bad"), course("3", active=False)]
    )
    assert [(d.kind, d.source_id) for d in ds] == [("assignment", "a"), ("quiz", "q")]
    assert [e["course_id"] for e in errors] == ["2"]
    assert [c.id for c in active] == ["1", "2"]


def test_buckets_use_waterloo_local_date_and_ungraded_rule():
    ds, _, _ = deadlines(
        FakeClient(
            assignments={
                "1": [
                    assignment("late-ungraded", NOW - timedelta(days=2), "Late"),
                    assignment("late-graded", NOW - timedelta(days=2), "Done", earned=8),
                    assignment("ancient", NOW - timedelta(days=60), "Ancient"),
                    assignment("tonight", NOW + timedelta(hours=9), "Tonight"),  # 19:00 Toronto, same local day
                    assignment("tomorrow", NOW + timedelta(hours=15), "Tomorrow"),  # 01:00 Toronto, next local day
                    assignment("later", NOW + timedelta(days=5), "Later"),
                    assignment("far", NOW + timedelta(days=30), "Far"),
                ]
            },
            quizzes={"1": [quiz("q-past", NOW - timedelta(days=1), "Past quiz")]},
        )
    )
    b = today.build_today(ds, NOW)
    assert [r["name"] for r in b["past_due"]] == ["Late"]  # graded, ancient and quizzes excluded
    assert [r["name"] for r in b["today"]] == ["Tonight"]
    assert [r["name"] for r in b["upcoming"]] == ["Tomorrow", "Later"]


def test_render_flags_failed_courses_and_overdue_caveat():
    ds, errs, _ = today.collect_deadlines(
        FakeClient(assignments={"1": [assignment("a", NOW - timedelta(days=1), "Late")], "2": LearnApiError("nope")}),
        [course("1"), course("2", "Broken")],
    )
    text = today.render_today(today.build_today(ds, NOW), errs, NOW)
    assert "Past due, not yet graded" in text and "[MSE 232] Late" in text
    assert "does not expose whether you submitted" in text
    assert "Could not load Broken" in text and "NOT shown" in text
    assert "Wednesday, October 14" in text


def test_ics_structure_crlf_and_deterministic():
    ds, _, _ = deadlines(FakeClient(assignments={"1": [assignment("a", NOW + timedelta(days=1), "HW 1; part, two")]}))
    ics = today.to_ics(ds, NOW)
    assert ics == today.to_ics(ds, NOW)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "\n" not in ics.replace("\r\n", "")
    assert r"SUMMARY:MSE 232: HW 1\; part\, two (due)" in ics
    assert "DTSTART:20261015T133000Z" in ics and "DTEND:20261015T140000Z" in ics
    assert "DTSTAMP:20261014T140000Z" in ics


def test_ics_uid_is_stable_across_edits_but_unique_per_item():
    a = today.collect_deadlines(FakeClient(assignments={"1": [assignment("a", NOW + timedelta(days=1), "Old name")]}), [course()])[0]
    b = today.collect_deadlines(FakeClient(assignments={"1": [assignment("a", NOW + timedelta(days=3), "New name")]}), [course()])[0]
    c = today.collect_deadlines(FakeClient(assignments={"1": [assignment("other", NOW + timedelta(days=1))]}), [course()])[0]
    uid = lambda ds: [l for l in today.to_ics(ds, NOW).split("\r\n") if l.startswith("UID:")][0]
    assert uid(a) == uid(b) != uid(c)


def test_ics_lines_are_folded_to_75_octets_without_splitting_characters():
    long_name = "Écrire un très long titre d'évaluation — " * 6
    ds, _, _ = deadlines(FakeClient(assignments={"1": [assignment("a", NOW + timedelta(days=1), long_name)]}))
    physical = today.to_ics(ds, NOW).split("\r\n")
    assert all(len(l.encode("utf-8")) <= 75 for l in physical)
    unfolded = "".join(l[1:] if l.startswith(" ") else "\n" + l for l in physical)
    assert long_name.replace(",", "\\,") in unfolded  # content survives folding intact


def test_ics_has_no_event_for_nothing():
    ics = today.to_ics([], NOW)
    assert "BEGIN:VEVENT" not in ics and "END:VCALENDAR" in ics
