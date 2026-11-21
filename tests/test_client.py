import pytest

from learn.client import LearnClient
from learn.exceptions import LearnApiError, LearnAuthenticationError, LearnToolUnavailableError
from tests.fake_valence import load_fixture, make_transport


def client_with(extra_routes: dict) -> LearnClient:
    return LearnClient(make_transport(extra_routes))


# ---- Courses -----------------------------------------------------------

def test_get_courses_normal():
    client = client_with({"enrollments/myenrollments": (200, load_fixture("enrollments.json"))})
    courses = client.get_courses()

    # The "Organization" (Type.Id == 1) item must be filtered out.
    assert len(courses) == 1
    course = courses[0]
    assert course.id == "1000001"
    assert course.code == "WIDG101_smith_1265"
    assert course.name == "Intro to Widgets - Spring 2026"
    assert course.is_active is True
    assert course.url == "https://learn.uwaterloo.ca/d2l/home/1000001"
    assert course.last_accessed_at is not None
    assert course.last_accessed_at.tzinfo is not None


# ---- Assignments ---------------------------------------------------------

def test_get_assignments_normal_with_grade():
    client = client_with(
        {
            "dropbox/folders": (200, load_fixture("dropbox_folders.json")),
            "grades/values/myGradeValues": (200, load_fixture("grades_values.json")),
        }
    )
    assignments = client.get_assignments("1000001")
    assert len(assignments) == 2

    graded = next(a for a in assignments if a.id == "500001")
    assert graded.name == "Assignment 1 - Basics"
    assert graded.max_points == 10.0
    assert graded.points_earned == 8.5
    assert graded.points_possible == 10.0
    assert graded.description == "<p>Submit as PDF.</p>"


def test_assignment_with_no_due_date():
    client = client_with(
        {
            "dropbox/folders": (200, load_fixture("dropbox_folders.json")),
            "grades/values/myGradeValues": (200, load_fixture("grades_values.json")),
        }
    )
    assignments = client.get_assignments("1000001")
    no_due_date = next(a for a in assignments if a.id == "500002")
    assert no_due_date.due_at is None


def test_assignment_with_no_grade():
    client = client_with(
        {
            "dropbox/folders": (200, load_fixture("dropbox_folders.json")),
            "grades/values/myGradeValues": (200, load_fixture("grades_values.json")),
        }
    )
    assignments = client.get_assignments("1000001")
    ungraded = next(a for a in assignments if a.id == "500002")
    assert ungraded.points_earned is None
    assert ungraded.points_possible is None
    assert ungraded.description is None  # empty CustomInstructions -> None


def test_assignment_missing_optional_fields_does_not_crash():
    minimal_folder = [
        {
            "Id": 500099,
            "Name": "Bare-bones folder",
            "GradeItemId": None,
            # No "Assessment", "CustomInstructions", "DueDate", "IsHidden" keys at all.
        }
    ]
    client = client_with(
        {
            "dropbox/folders": (200, minimal_folder),
            "grades/values/myGradeValues": (200, []),
        }
    )
    assignments = client.get_assignments("1000001")
    assert len(assignments) == 1
    a = assignments[0]
    assert a.max_points is None
    assert a.description is None
    assert a.due_at is None
    assert a.is_hidden is False
    assert a.grade_item_id is None


# ---- Grades ---------------------------------------------------------------

def test_get_grades_normal():
    client = client_with({"grades/values/myGradeValues": (200, load_fixture("grades_values.json"))})
    grades = client.get_grades("1000001")
    assert len(grades) == 1
    grade = grades[0]
    assert grade.id == "700001"
    assert grade.points_earned == 8.5
    assert grade.points_possible == 10.0
    assert grade.percentage == 85.0
    assert grade.displayed_grade == "85 %"
    assert grade.comments == "<p>Good work.</p>"
    assert grade.letter_grade is None
    assert grade.graded_at.tzinfo is not None


# ---- Quizzes ----------------------------------------------------------------

def test_get_quizzes_normal():
    client = client_with({"/quizzes/": (200, load_fixture("quizzes.json"))})
    quizzes = client.get_quizzes("1000001")
    assert len(quizzes) == 2

    with_description = next(q for q in quizzes if q.id == "800001")
    assert with_description.description == "<p>15 minutes, one attempt.</p>"
    assert with_description.is_active is True

    without = next(q for q in quizzes if q.id == "800002")
    assert without.description is None
    assert without.grade_item_id is None
    assert without.is_active is False


# ---- Announcements ------------------------------------------------------

def test_get_announcements_normal():
    client = client_with({"/news/": (200, load_fixture("announcements.json"))})
    announcements = client.get_announcements("1000001")
    assert len(announcements) == 1
    a = announcements[0]
    assert a.title == "Final Exam Updates"
    assert a.body == "<p>See attached schedule.</p>"
    assert a.created_at.tzinfo is not None
    assert a.updated_at.tzinfo is not None


def test_get_announcements_empty_result():
    client = client_with({"/news/": (200, [])})
    assert client.get_announcements("1000001") == []


# ---- Content --------------------------------------------------------------

def test_get_course_content_normal_and_flattening():
    client = client_with({"/content/toc": (200, load_fixture("content_toc.json"))})
    items = client.get_course_content("1000001")
    assert len(items) == 3

    by_id = {item.id: item for item in items}

    outline = by_id["610001"]
    assert outline.module_title == "Week 1"
    assert outline.url == "https://outline.uwaterloo.ca/viewer/view/example"

    slides = by_id["610002"]
    assert slides.module_title == "Week 1"
    assert slides.url is None  # missing "Url" key entirely in the fixture
    assert slides.description is None
    assert slides.grade_item_id is None

    bonus = by_id["610003"]
    assert bonus.module_title == "Week 2 - Optional Extras"  # nested submodule
    assert bonus.grade_item_id == "700010"


# ---- Error handling ---------------------------------------------------------

def test_tool_unavailable_returns_empty_list():
    client = client_with({"dropbox/folders": (403, load_fixture("error_not_authorized.json"))})
    assert client.get_assignments("1000001") == []


def test_authentication_error_raises_not_empty_list():
    client = client_with({"dropbox/folders": (401, {})})
    with pytest.raises(LearnAuthenticationError):
        client.get_assignments("1000001")


def test_other_api_error_raises():
    client = client_with({"dropbox/folders": (500, {})})
    with pytest.raises(LearnApiError):
        client.get_assignments("1000001")


def test_error_body_shape_without_error_status_still_raises():
    # Defensive path: 200 status but an {"Errors": [...]} body.
    client = client_with({"dropbox/folders": (200, {"Errors": [{"Message": "Not Authorized"}]})})
    with pytest.raises(LearnToolUnavailableError):
        client._get(f"https://learn.uwaterloo.ca/d2l/api/le/1.96/1000001/dropbox/folders/")
