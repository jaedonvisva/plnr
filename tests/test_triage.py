import sys
import types
from datetime import datetime, timezone

import pytest

from agent import triage
from agent.tools import build_tools
from learn.exceptions import LearnApiError
from learn.models import Announcement, Course


def _course(cid, active=True):
    return Course(
        id=cid,
        name=f"Course {cid}",
        code=f"C {cid}",
        is_active=active,
        url=f"https://learn.example/{cid}",
        last_accessed_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        raw=None,
    )


def _ann(aid, course_id, title, body):
    return Announcement(
        id=aid,
        course_id=course_id,
        title=title,
        body=body,
        is_hidden=False,
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        updated_at=None,
        raw={"CreatedBy": 1},
    )


class FakeDecider:
    """Returns canned probabilities keyed by a substring of the title."""

    def __init__(self, table, default=None):
        self.table = table
        self.default = default or {"schedule_change": 0.0, "action_required": 0.0, "routine": 1.0}
        self.calls = []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        for needle, probs in self.table.items():
            if needle in state["title"]:
                return probs
        return self.default


class FakeClient:
    def __init__(self, by_course, failing=()):
        self.by_course = by_course
        self.failing = set(failing)

    def get_announcements(self, course_id):
        if course_id in self.failing:
            raise LearnApiError("boom")
        return self.by_course.get(course_id, [])


EXAM = {"schedule_change": 0.9, "action_required": 0.8, "routine": 0.1}
ROUTINE = {"schedule_change": 0.05, "action_required": 0.1, "routine": 0.95}
HOMEWORK = {"schedule_change": 0.1, "action_required": 0.7, "routine": 0.3}


def test_score_combines_importance_and_routine_penalty():
    assert triage.combine_score(EXAM) == pytest.approx(0.9 * (1 - 0.05))
    assert triage.combine_score(ROUTINE) == pytest.approx(0.1 * (1 - 0.475))
    assert triage.combine_score(EXAM) > triage.combine_score(HOMEWORK) > triage.combine_score(ROUTINE)


def test_calibrate_is_identity_at_temperature_one_and_softens_when_hot():
    assert triage.calibrate(0.9, temperature=1.0) == pytest.approx(0.9)
    assert triage.calibrate(0.9, temperature=2.0) < 0.9
    assert triage.calibrate(0.5, temperature=3.0) == pytest.approx(0.5)
    # edges must not blow up (log(0))
    assert 0.0 <= triage.calibrate(0.0, temperature=2.0) <= 1.0
    assert 0.0 <= triage.calibrate(1.0, temperature=2.0) <= 1.0


def test_triage_returns_only_items_above_threshold_sorted_by_score():
    client = FakeClient(
        {
            "1": [_ann("a", "1", "Welcome back", "<p>hi</p>"), _ann("b", "1", "Midterm moved", "<p>Room change</p>")],
            "2": [_ann("c", "2", "Homework posted", "<p>do it</p>")],
        }
    )
    decider = FakeDecider({"Midterm": EXAM, "Homework": HOMEWORK, "Welcome": ROUTINE})
    result = triage.triage_announcements(client, [_course("1"), _course("2")], decider, None, min_score=0.5, limit=10)

    assert [r["id"] for r in result["results"]] == ["b", "c"]
    assert result["results"][0]["score"] > result["results"][1]["score"]
    assert result["results"][0]["signals"] == EXAM
    assert {c["course_id"] for c in result["courses_checked"]} == {"1", "2"}
    assert result["errors"] == []


def test_triage_applies_limit():
    anns = [_ann(str(i), "1", f"Exam {i}", "<p>x</p>") for i in range(5)]
    client = FakeClient({"1": anns})
    result = triage.triage_announcements(client, [_course("1")], FakeDecider({"Exam": EXAM}), None, min_score=0.1, limit=2)
    assert len(result["results"]) == 2


def test_course_with_nothing_important_is_listed_not_dropped():
    client = FakeClient({"1": [_ann("a", "1", "Welcome", "<p>hi</p>")]})
    result = triage.triage_announcements(client, [_course("1")], FakeDecider({}), None, min_score=0.5, limit=10)
    assert result["results"] == []
    assert result["courses_with_no_results"] == [{"course_id": "1", "course_name": "Course 1"}]


def test_failing_course_goes_to_errors_and_others_continue():
    client = FakeClient({"2": [_ann("c", "2", "Exam time", "<p>x</p>")]}, failing={"1"})
    result = triage.triage_announcements(
        client, [_course("1"), _course("2")], FakeDecider({"Exam": EXAM}), None, min_score=0.5, limit=10
    )
    assert [e["course_id"] for e in result["errors"]] == ["1"]
    assert [r["id"] for r in result["results"]] == ["c"]


def test_unknown_course_id_returns_not_found_error():
    result = triage.triage_announcements(FakeClient({}), [_course("1")], FakeDecider({}), "999", min_score=0.5, limit=10)
    assert result["error"] is True
    assert result["type"] == "not_found"


def test_inactive_courses_are_skipped_when_no_course_id_given():
    client = FakeClient({"1": [_ann("a", "1", "Exam", "<p>x</p>")]})
    result = triage.triage_announcements(client, [_course("1", active=False)], FakeDecider({"Exam": EXAM}), None, 0.5, 10)
    assert result["courses_checked"] == []
    assert result["results"] == []


def test_model_sees_stripped_text_and_long_bodies_are_flagged_truncated():
    long_body = "<p>" + ("word " * 1000) + "</p>"
    client = FakeClient({"1": [_ann("a", "1", "Exam", long_body)]})
    decider = FakeDecider({"Exam": EXAM})
    result = triage.triage_announcements(client, [_course("1")], decider, None, 0.5, 10)

    state, _ = decider.calls[0]
    assert "<p>" not in state["body"]
    assert len(state["body"]) <= triage.MODEL_BODY_CHARS
    assert result["results"][0]["body_truncated_for_model"] is True


def test_results_are_marked_untrusted_and_flagged_as_model_estimates_without_raw():
    client = FakeClient({"1": [_ann("a", "1", "Exam", "<p>x</p>")]})
    result = triage.triage_announcements(client, [_course("1")], FakeDecider({"Exam": EXAM}), None, 0.5, 10)
    item = result["results"][0]
    assert item["_content_source"] == "learn_data_untrusted"
    assert item["source"] == "local_classifier_estimate"
    assert "raw" not in item


def test_only_the_three_triage_questions_are_asked_each_as_noul():
    client = FakeClient({"1": [_ann("a", "1", "Exam", "<p>x</p>")]})
    decider = FakeDecider({"Exam": EXAM})
    triage.triage_announcements(client, [_course("1")], decider, None, 0.5, 10)
    _, questions = decider.calls[0]
    assert set(questions) == {"schedule_change", "action_required", "routine"}
    assert all(q["type"] == "noul" for q in questions.values())


# ---- LayaDecider ----------------------------------------------------------


def _install_stub_laya(monkeypatch, answers):
    created = []

    class Router:
        def __init__(self, *a, **kw):
            created.append(1)

        def predict(self, state, questions):
            return {"answers": {qid: {"noul": p} for qid, p in answers.items()}, "routing": {}}

    monkeypatch.setitem(sys.modules, "laya", types.SimpleNamespace(Router=Router))
    return created


def test_laya_decider_is_lazy_and_builds_router_once(monkeypatch):
    created = _install_stub_laya(monkeypatch, {"schedule_change": 0.9, "action_required": 0.2, "routine": 0.1})
    decider = triage.LayaDecider()
    assert created == []  # nothing loaded at construction
    out = decider.decide({"title": "t", "body": "b"}, triage.QUESTIONS)
    decider.decide({"title": "t", "body": "b"}, triage.QUESTIONS)
    assert created == [1]
    assert out["schedule_change"] == pytest.approx(0.9)


def test_default_decider_is_none_without_laya(monkeypatch):
    monkeypatch.setitem(sys.modules, "laya", None)  # makes `import laya` / find_spec fail
    assert triage.default_decider() is None


def test_default_decider_is_a_laya_decider_when_importable(monkeypatch):
    _install_stub_laya(monkeypatch, {})
    assert isinstance(triage.default_decider(), triage.LayaDecider)


# ---- The tool -------------------------------------------------------------


class ToolClient(FakeClient):
    def get_courses(self):
        return [_course("1")]


def _tools(decider=None, announcements=None):
    anns = announcements if announcements is not None else [_ann("b", "1", "Exam moved", "<p>x</p>")]
    return build_tools(ToolClient({"1": anns}), decider=decider)


def test_tool_absent_without_a_decider():
    assert "get_important_announcements" not in _tools(decider=None)


def test_tool_present_with_a_decider_and_returns_results():
    tools = _tools(decider=FakeDecider({"Exam": EXAM}))
    result = tools["get_important_announcements"]()
    assert [r["id"] for r in result["results"]] == ["b"]


def test_tool_rejects_unknown_course_id():
    tools = _tools(decider=FakeDecider({}))
    result = tools["get_important_announcements"](course_id="999")
    assert result["error"] is True and result["type"] == "invalid_course_id"


@pytest.mark.parametrize("bad", [-0.1, 1.5, "0.5", True, None])
def test_tool_rejects_bad_min_score(bad):
    tools = _tools(decider=FakeDecider({}))
    result = tools["get_important_announcements"](min_score=bad)
    assert result["error"] is True


@pytest.mark.parametrize("bad", [0, 51, 2.5, "3", True])
def test_tool_rejects_bad_limit(bad):
    tools = _tools(decider=FakeDecider({}))
    result = tools["get_important_announcements"](limit=bad)
    assert result["error"] is True


def test_tool_reports_triage_unavailable_when_decider_raises():
    class Boom:
        def decide(self, state, questions):
            raise RuntimeError("weights missing")

    tools = _tools(decider=Boom())
    result = tools["get_important_announcements"]()
    assert result["error"] is True
    assert result["type"] == "triage_unavailable"


# ---- eval script ----------------------------------------------------------


def test_eval_script_runs_against_the_seed_fixture_with_a_fake_decider():
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("eval_triage", root / "scripts" / "eval_triage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    items = json.loads((root / "tests" / "fixtures" / "triage_labeled.json").read_text())["items"]
    assert len(items) >= 10 and {i["important"] for i in items} == {True, False}

    class Oracle:  # perfect decider: important titles get high signals
        important = {i["title"] for i in items if i["important"]}

        def decide(self, state, questions):
            hit = state["title"] in self.important
            return {"schedule_change": 0.9 if hit else 0.05, "action_required": 0.8 if hit else 0.05, "routine": 0.1 if hit else 0.9}

    report = mod.evaluate(Oracle(), items)
    assert "threshold" in report and "log-loss" in report
    scores = [mod.score_at(s, 1.0) for s in mod.raw_signals(Oracle(), items)]
    labels = [i["important"] for i in items]
    assert mod.prf(scores, labels, 0.5) == (1.0, 1.0, 1.0)
