from datetime import datetime, timedelta, timezone

import pytest

from agent import aggregation
from learn.exceptions import LearnApiError
from learn.models import Announcement, Assignment, ContentItem, Course, Quiz


def make_course(id_, name="Course", is_active=True):
    return Course(
        id=id_,
        name=name,
        code=f"CODE {id_}",
        is_active=is_active,
        url=f"https://learn.uwaterloo.ca/d2l/home/{id_}",
        last_accessed_at=None,
        raw=None,
    )


def make_assignment(id_, course_id, due_at, name="Assignment"):
    return Assignment(
        id=id_,
        course_id=course_id,
        name=name,
        description=None,
        due_at=due_at,
        max_points=100,
        grade_item_id=None,
        points_earned=None,
        points_possible=None,
        is_hidden=False,
        raw=None,
    )


def make_quiz(id_, course_id, due_at, name="Quiz"):
    return Quiz(
        id=id_,
        course_id=course_id,
        name=name,
        is_active=True,
        grade_item_id=None,
        description=None,
        due_at=due_at,
        raw=None,
    )


def make_announcement(id_, course_id, title, body):
    return Announcement(
        id=id_,
        course_id=course_id,
        title=title,
        body=body,
        is_hidden=False,
        created_at=None,
        updated_at=None,
        raw=None,
    )


def make_content_item(id_, course_id, module_title, title, description=None):
    return ContentItem(
        id=id_,
        course_id=course_id,
        module_title=module_title,
        title=title,
        type="Link",
        url=None,
        description=description,
        grade_item_id=None,
        updated_at=None,
        raw=None,
    )


class FakeClient:
    """Configurable per-course fake, keyed by course_id. Raising an entry
    for a course simulates that course's endpoint failing."""

    def __init__(self, assignments=None, announcements=None, content=None, quizzes=None):
        self.quizzes = quizzes or {}
        self.assignments = assignments or {}
        self.announcements = announcements or {}
        self.content = content or {}

    def get_assignments(self, course_id):
        value = self.assignments.get(course_id, [])
        if isinstance(value, Exception):
            raise value
        return value

    def get_quizzes(self, course_id):
        value = self.quizzes.get(course_id, [])
        if isinstance(value, Exception):
            raise value
        return value

    def get_announcements(self, course_id):
        value = self.announcements.get(course_id, [])
        if isinstance(value, Exception):
            raise value
        return value

    def get_course_content(self, course_id):
        value = self.content.get(course_id, [])
        if isinstance(value, Exception):
            raise value
        return value


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


# ---- collect_upcoming_assignments --------------------------------------

def test_upcoming_assignments_filters_by_window_and_sorts():
    courses = [make_course("1"), make_course("2")]
    client = FakeClient(
        assignments={
            "1": [
                make_assignment("a1", "1", NOW + timedelta(days=20), "Too far out"),
                make_assignment("a2", "1", NOW + timedelta(days=2), "Due soon"),
            ],
            "2": [make_assignment("a3", "2", NOW + timedelta(days=1), "Due sooner")],
        }
    )
    result = aggregation.collect_upcoming_assignments(client, courses, days_ahead=7, now=NOW)

    names = [item["name"] for item in result["items"]]
    assert names == ["Due sooner", "Due soon"]  # sorted by due_at, "Too far out" excluded
    assert result["courses_checked"] == [
        {"course_id": "1", "course_name": "Course"},
        {"course_id": "2", "course_name": "Course"},
    ]
    assert result["courses_with_no_results"] == []
    assert result["errors"] == []


def test_upcoming_assignments_excludes_past_due_items():
    courses = [make_course("1")]
    client = FakeClient(assignments={"1": [make_assignment("a1", "1", NOW - timedelta(days=1), "Already due")]})
    result = aggregation.collect_upcoming_assignments(client, courses, days_ahead=7, now=NOW)
    assert result["items"] == []
    assert result["courses_with_no_results"] == [{"course_id": "1", "course_name": "Course"}]


def test_upcoming_assignments_excludes_items_with_no_due_date():
    courses = [make_course("1")]
    client = FakeClient(assignments={"1": [make_assignment("a1", "1", None, "No due date")]})
    result = aggregation.collect_upcoming_assignments(client, courses, days_ahead=7, now=NOW)
    assert result["items"] == []


def test_upcoming_assignments_excludes_inactive_courses():
    courses = [make_course("1", is_active=False)]
    client = FakeClient(assignments={"1": [make_assignment("a1", "1", NOW + timedelta(days=1))]})
    result = aggregation.collect_upcoming_assignments(client, courses, days_ahead=7, now=NOW)
    assert result["courses_checked"] == []
    assert result["items"] == []


def test_upcoming_assignments_reports_per_course_error_without_aborting():
    courses = [make_course("1"), make_course("2")]
    client = FakeClient(
        assignments={
            "1": LearnApiError("boom", status=500, url="http://x"),
            "2": [make_assignment("a1", "2", NOW + timedelta(days=1), "Still works")],
        }
    )
    result = aggregation.collect_upcoming_assignments(client, courses, days_ahead=7, now=NOW)
    assert result["errors"] == [{"course_id": "1", "course_name": "Course", "message": "boom"}]
    assert [item["name"] for item in result["items"]] == ["Still works"]


def test_upcoming_assignments_items_have_provenance():
    courses = [make_course("1")]
    client = FakeClient(assignments={"1": [make_assignment("a1", "1", NOW + timedelta(days=1))]})
    result = aggregation.collect_upcoming_assignments(client, courses, days_ahead=7, now=NOW)
    item = result["items"][0]
    assert item["source"] == "structured_assignment_data"
    assert item["verified_structured_assignment"] is True


def test_upcoming_note_explains_quiz_coverage():
    result = aggregation.collect_upcoming_assignments(FakeClient(), [], days_ahead=7, now=NOW)
    assert "quiz" in result["note"].lower() and "duedate" in result["note"].lower()


def test_upcoming_includes_quizzes_with_due_dates_in_window_sorted_with_assignments():
    courses = [make_course("1")]
    client = FakeClient(
        assignments={"1": [make_assignment("a1", "1", NOW + timedelta(days=3), "HW")]},
        quizzes={
            "1": [
                make_quiz("q1", "1", NOW + timedelta(days=1), "Quiz soon"),
                make_quiz("q2", "1", NOW + timedelta(days=30), "Quiz far"),
                make_quiz("q3", "1", None, "Quiz undated"),
            ]
        },
    )
    result = aggregation.collect_upcoming_assignments(client, courses, days_ahead=7, now=NOW)
    assert [(i["type"], i["name"]) for i in result["items"]] == [("quiz", "Quiz soon"), ("assignment", "HW")]
    assert result["items"][0]["source"] == "structured_quiz_data"
    assert result["courses_with_no_results"] == []


def test_course_with_only_an_in_window_quiz_is_not_reported_as_empty():
    client = FakeClient(quizzes={"1": [make_quiz("q1", "1", NOW + timedelta(days=2))]})
    result = aggregation.collect_upcoming_assignments(client, [make_course("1")], days_ahead=7, now=NOW)
    assert len(result["items"]) == 1 and result["courses_with_no_results"] == []


def test_quiz_fetch_error_is_reported_per_course_without_aborting_others():
    courses = [make_course("1"), make_course("2")]
    client = FakeClient(
        assignments={"2": [make_assignment("a", "2", NOW + timedelta(days=1))]},
        quizzes={"1": LearnApiError("boom")},
    )
    result = aggregation.collect_upcoming_assignments(client, courses, days_ahead=7, now=NOW)
    assert [e["course_id"] for e in result["errors"]] == ["1"]
    assert len(result["items"]) == 1


# ---- search_announcements ------------------------------------------------

def test_search_announcements_matches_title_and_full_body_case_insensitive():
    courses = [make_course("1")]
    client = FakeClient(
        announcements={
            "1": [
                make_announcement("n1", "1", "Midterm info", "<p>details</p>"),
                make_announcement("n2", "1", "Reminder", "<p>The EXAM is Friday</p>"),
                make_announcement("n3", "1", "Unrelated", "<p>nothing here</p>"),
            ]
        }
    )
    result = aggregation.search_announcements(client, courses, "exam", course_id=None)
    ids = {r["id"] for r in result["results"]}
    assert ids == {"n2"}


def test_search_announcements_searches_full_body_before_truncation():
    courses = [make_course("1")]
    long_body = "<p>" + ("filler " * 200) + "needle" + "</p>"
    client = FakeClient(announcements={"1": [make_announcement("n1", "1", "Title", long_body)]})
    result = aggregation.search_announcements(client, courses, "needle", course_id=None)
    assert len(result["results"]) == 1


def test_search_announcements_scopes_to_one_course():
    courses = [make_course("1"), make_course("2")]
    client = FakeClient(
        announcements={
            "1": [make_announcement("n1", "1", "exam notice", "")],
            "2": [make_announcement("n2", "2", "exam notice too", "")],
        }
    )
    result = aggregation.search_announcements(client, courses, "exam", course_id="1")
    assert result["courses_checked"] == [{"course_id": "1", "course_name": "Course"}]
    assert [r["id"] for r in result["results"]] == ["n1"]


def test_search_announcements_nonexistent_course_returns_not_found_error():
    result = aggregation.search_announcements(FakeClient(), [make_course("1")], "exam", course_id="does-not-exist")
    assert result["error"] is True
    assert result["type"] == "not_found"


def test_search_announcements_reports_courses_with_no_matches():
    courses = [make_course("1")]
    client = FakeClient(announcements={"1": [make_announcement("n1", "1", "unrelated", "")]})
    result = aggregation.search_announcements(client, courses, "exam", course_id=None)
    assert result["results"] == []
    assert result["courses_with_no_results"] == [{"course_id": "1", "course_name": "Course"}]


def test_search_announcements_results_exclude_raw():
    courses = [make_course("1")]
    ann = Announcement(
        id="n1",
        course_id="1",
        title="exam",
        body="",
        is_hidden=False,
        created_at=None,
        updated_at=None,
        raw={"secret": "should not appear"},
    )
    client = FakeClient(announcements={"1": [ann]})
    result = aggregation.search_announcements(client, courses, "exam", course_id=None)
    assert "raw" not in result["results"][0]
    assert "should not appear" not in str(result)


# ---- search_course_content -----------------------------------------------

def test_search_course_content_matches_title_module_and_description():
    courses = [make_course("1")]
    client = FakeClient(
        content={
            "1": [
                make_content_item("c1", "1", "Week 1", "Optimization intro"),
                make_content_item("c2", "1", "Optimization Module", "Something else"),
                make_content_item("c3", "1", "Week 2", "unrelated", description="<p>talks about optimization</p>"),
                make_content_item("c4", "1", "Week 3", "unrelated"),
            ]
        }
    )
    result = aggregation.search_course_content(client, courses, "optimization", course_id=None)
    ids = {r["id"] for r in result["results"]}
    assert ids == {"c1", "c2", "c3"}


def test_search_course_content_nonexistent_course_returns_not_found_error():
    result = aggregation.search_course_content(FakeClient(), [make_course("1")], "x", course_id="nope")
    assert result["error"] is True
    assert result["type"] == "not_found"


def test_search_course_content_error_reported_per_course():
    courses = [make_course("1")]
    client = FakeClient(content={"1": LearnApiError("down", status=500, url="http://x")})
    result = aggregation.search_course_content(client, courses, "optimization", course_id=None)
    assert result["errors"] == [{"course_id": "1", "course_name": "Course", "message": "down"}]
    assert result["results"] == []
