from datetime import datetime, timedelta, timezone

from agent.tools import build_tools
from learn.models import Announcement, Assignment, ContentItem, Course


class FakeLearnClient:
    """Stands in for LearnClient -- the tool layer must not care that this
    isn't the real thing, since it never touches Playwright/BrowserContext
    itself."""

    def __init__(self):
        self.get_assignments_calls = []

    def get_courses(self):
        return [
            Course(
                id="1",
                name="Algorithms and Data Structures",
                code="MSE 240",
                is_active=True,
                url="https://learn.uwaterloo.ca/d2l/home/1",
                last_accessed_at=datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
                raw={"OrgUnit": {"secret": "should not appear"}},
            )
        ]

    def get_assignments(self, course_id):
        self.get_assignments_calls.append(course_id)
        return []

    def get_grades(self, course_id):
        return []

    def get_quizzes(self, course_id):
        return []

    def get_announcements(self, course_id):
        return [
            Announcement(
                id="1",
                course_id=course_id,
                title="Welcome",
                body="<p>hi</p>",
                is_hidden=False,
                created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                updated_at=None,
                raw={"CreatedBy": 999999},
            )
        ]

    def get_course_content(self, course_id):
        return [
            ContentItem(
                id="c1",
                course_id=course_id,
                module_title="Week 1",
                title="Optimization intro",
                type="Link",
                url=None,
                description=None,
                grade_item_id=None,
                updated_at=None,
                raw=None,
            )
        ]


def test_build_tools_returns_expected_names():
    tools = build_tools(FakeLearnClient())
    assert set(tools) == {
        "get_courses",
        "get_assignments",
        "get_grades",
        "get_quizzes",
        "get_announcements",
        "get_announcement_detail",
        "get_course_content",
        "get_upcoming_items",
        "search_announcements",
        "search_course_content",
    }


def test_get_courses_serializes_to_plain_dict_without_raw():
    tools = build_tools(FakeLearnClient())
    result = tools["get_courses"]()
    assert result == [
        {
            "_content_source": "learn_data_untrusted",
            "id": "1",
            "name": "Algorithms and Data Structures",
            "code": "MSE 240",
            "is_active": True,
            "url": "https://learn.uwaterloo.ca/d2l/home/1",
            "last_accessed_at": "2026-08-18T12:00:00+00:00",
        }
    ]
    # No dataclass or datetime object should have leaked through, and the
    # internal `raw` field (which could carry extra D2L data) must be gone.
    assert "raw" not in result[0]
    assert isinstance(result[0]["last_accessed_at"], str)


def test_get_assignments_passes_course_id_through():
    client = FakeLearnClient()
    tools = build_tools(client)
    result = tools["get_assignments"]("1")
    assert result == []
    assert client.get_assignments_calls == ["1"]


def test_get_announcements_excludes_raw_and_strips_html_body():
    tools = build_tools(FakeLearnClient())
    result = tools["get_announcements"]("1")
    assert result == [
        {
            "_content_source": "learn_data_untrusted",
            "id": "1",
            "course_id": "1",
            "title": "Welcome",
            "body": "hi",  # HTML stripped, short enough to not need truncation
            "is_hidden": False,
            "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": None,
        }
    ]
    assert "raw" not in result[0]
    assert "999999" not in str(result)


def test_get_announcements_truncates_long_body():
    class LongBodyClient(FakeLearnClient):
        def get_announcements(self, course_id):
            return [
                Announcement(
                    id="1",
                    course_id=course_id,
                    title="Long",
                    body="<p>" + ("word " * 200) + "</p>",  # ~1000 chars, over the 500-char cap
                    is_hidden=False,
                    created_at=None,
                    updated_at=None,
                    raw=None,
                )
            ]

    tools = build_tools(LongBodyClient())
    result = tools["get_announcements"]("1")
    body = result[0]["body"]
    assert len(body) < 1000
    assert body.endswith("...[truncated -- use get_announcement_detail for the full text]")


def test_get_announcement_detail_returns_full_untruncated_body():
    class LongBodyClient(FakeLearnClient):
        def get_announcements(self, course_id):
            return [
                Announcement(
                    id="7",
                    course_id=course_id,
                    title="Long",
                    body="<p>" + ("word " * 200) + "</p>",
                    is_hidden=False,
                    created_at=None,
                    updated_at=None,
                    raw=None,
                )
            ]

    tools = build_tools(LongBodyClient())
    result = tools["get_announcement_detail"]("1", "7")
    assert "...[truncated" not in result["body"]
    assert result["body"] == ("word " * 200).strip()


def test_get_announcement_detail_not_found_returns_error():
    tools = build_tools(FakeLearnClient())
    result = tools["get_announcement_detail"]("1", "does-not-exist")
    assert result["error"] is True
    assert result["type"] == "not_found"


# ---- Whitelist / security -------------------------------------------------

def test_tool_registry_does_not_expose_write_capable_or_unlisted_client_methods():
    """The tool registry is an explicit whitelist (see agent/tools.py module
    docstring): only functions defined inside build_tools() are ever
    reachable by the model, regardless of what LearnClient itself has --
    e.g. get_discussion_forums exists on LearnClient but was never added
    here, so it must not appear."""
    tools = build_tools(FakeLearnClient())
    assert "get_discussion_forums" not in tools


def test_no_tool_result_leaks_raw_field_or_secrets():
    tools = build_tools(FakeLearnClient())
    for name, fn in tools.items():
        import inspect

        sig = inspect.signature(fn)
        kwargs = {}
        for pname, param in sig.parameters.items():
            if param.default is not inspect._empty:
                continue
            kwargs[pname] = "1"
        result = fn(**kwargs)
        blob = str(result)
        assert "should not appear" not in blob
        assert "999999" not in blob
        assert '"raw"' not in blob


# ---- get_upcoming_items ----------------------------------------------------

def test_get_upcoming_items_returns_structured_cross_course_result():
    class WithUpcomingAssignment(FakeLearnClient):
        def get_assignments(self, course_id):
            self.get_assignments_calls.append(course_id)
            return [
                Assignment(
                    id="a1",
                    course_id=course_id,
                    name="Upcoming assignment",
                    description=None,
                    due_at=datetime.now(timezone.utc) + timedelta(days=3),
                    max_points=100,
                    grade_item_id=None,
                    points_earned=None,
                    points_possible=None,
                    is_hidden=False,
                    raw=None,
                )
            ]

    tools = build_tools(WithUpcomingAssignment())
    result = tools["get_upcoming_items"]()
    assert result["courses_checked"] == [{"course_id": "1", "course_name": "Algorithms and Data Structures"}]
    assert len(result["items"]) == 1
    assert result["items"][0]["source"] == "structured_assignment_data"


def test_get_upcoming_items_rejects_non_positive_days_ahead():
    tools = build_tools(FakeLearnClient())
    for bad in (0, -5):
        result = tools["get_upcoming_items"](days_ahead=bad)
        assert result["error"] is True
        assert result["type"] == "tool_error"


# ---- search_announcements ---------------------------------------------------

def test_search_announcements_tool_rejects_empty_query():
    tools = build_tools(FakeLearnClient())
    result = tools["search_announcements"]("")
    assert result["error"] is True
    assert result["type"] == "tool_error"


def test_search_announcements_tool_finds_match_across_courses():
    tools = build_tools(FakeLearnClient())
    result = tools["search_announcements"]("welcome")
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "Welcome"


def test_search_announcements_tool_nonexistent_course_returns_not_found():
    tools = build_tools(FakeLearnClient())
    result = tools["search_announcements"]("welcome", course_id="does-not-exist")
    assert result["error"] is True
    assert result["type"] == "invalid_course_id"


# ---- search_course_content --------------------------------------------------

def test_search_course_content_tool_rejects_empty_query():
    tools = build_tools(FakeLearnClient())
    result = tools["search_course_content"]("")
    assert result["error"] is True
    assert result["type"] == "tool_error"


def test_search_course_content_tool_finds_match():
    tools = build_tools(FakeLearnClient())
    result = tools["search_course_content"]("optimization")
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "Optimization intro"
