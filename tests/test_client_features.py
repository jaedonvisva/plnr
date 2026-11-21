"""Quiz dates, pagination/completeness, term parsing, attachments."""

import pytest

from learn import terms
from learn.client import BASE_URL, MAX_PAGES, LearnClient
from learn.exceptions import LearnApiError
from tests.fake_valence import load_fixture, make_transport

ORG = "1000001"
NEWS = f"/d2l/api/le/1.96/{ORG}/news/"


def client_with(routes):
    return LearnClient(make_transport(routes))


# ---- terms ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,display,term,label",
    [
        ("MSE232_prof_1265", "MSE 232", "1265", "Spring 2026"),
        ("ECE105A_x_1259", "ECE 105A", "1259", "Fall 2025"),
        ("CS135_prof_1261", "CS 135", "1261", "Winter 2026"),
        ("WIDG101_smith_1265", "WIDG 101", "1265", "Spring 2026"),
        ("PD11_081_cel_1269", "PD 11", "1269", "Fall 2026"),  # real shape seen live: 2-digit number, section field
    ],
)
def test_course_and_term_parsing(code, display, term, label):
    assert terms.display_code(code) == display
    assert terms.term_from_code(code) == term
    assert terms.term_label(term) == label


@pytest.mark.parametrize("code", [None, "", "Orientation Module", "ESMS Safety Orientation", "Engineering Co-op Community", "12345", "X"])
def test_unparseable_codes_return_none_not_a_guess(code):
    assert terms.display_code(code) is None
    assert terms.term_from_code(code) is None


def test_term_label_rejects_unknown_season_and_range():
    assert terms.term_label("1267") is None  # month digit 7 isn't a Waterloo term
    assert terms.term_label("2265") is None
    assert terms.term_label(None) is None


def test_get_courses_populates_display_code_and_term():
    client = client_with({"enrollments/myenrollments": (200, load_fixture("enrollments.json"))})
    c = client.get_courses()[0]
    assert (c.display_code, c.term, c.term_label) == ("WIDG 101", "1265", "Spring 2026")


# ---- quiz dates ----------------------------------------------------------------------------


def test_quiz_dates_parsed_when_present_and_none_when_absent():
    quizzes = [
        {"QuizId": 1, "Name": "Q1", "IsActive": True, "GradeItemId": None, "Description": {"Text": {"Html": ""}},
         "DueDate": "2026-10-20T03:59:00.000Z", "StartDate": "2026-10-19T04:00:00.000Z", "EndDate": "2026-10-21T04:00:00.000Z"},
        {"QuizId": 2, "Name": "Q2", "IsActive": True, "GradeItemId": None, "Description": {"Text": {"Html": ""}}},
    ]
    client = client_with({"/quizzes/": (200, quizzes)})
    q1, q2 = client.get_quizzes(ORG)
    assert q1.due_at.isoformat() == "2026-10-20T03:59:00+00:00"
    assert q1.start_at is not None and q1.end_at is not None
    assert (q2.due_at, q2.start_at, q2.end_at) == (None, None, None)


# ---- pagination ----------------------------------------------------------------------------


class PagedTransport:
    """Serves pages in order for any URL; records requested URLs."""

    def __init__(self, pages, versions=True):
        self.pages = list(pages)
        self.urls = []
        self.versions = load_fixture("versions.json")

    def get(self, url):
        self.urls.append(url)
        from tests.fake_valence import FakeResponse

        if "/d2l/api/versions/" in url:
            return FakeResponse(200, self.versions)
        return FakeResponse(*self.pages.pop(0))


def _news(i):
    return {"Id": i, "Title": f"T{i}", "IsHidden": False, "Body": {"Html": ""}}


def test_objects_next_pagination_is_followed_to_the_end():
    t = PagedTransport(
        [
            (200, {"Objects": [_news(1), _news(2)], "Next": f"{BASE_URL}{NEWS}?page=2"}),
            (200, {"Objects": [_news(3)], "Next": None}),
        ]
    )
    items = LearnClient(t).get_announcements(ORG)
    assert [a.id for a in items] == ["1", "2", "3"]
    assert t.urls[-1].endswith("?page=2")


def test_items_pagininfo_bookmark_pagination_replaces_old_bookmark():
    t = PagedTransport(
        [
            (200, {"PagingInfo": {"HasMoreItems": True, "Bookmark": "b1"}, "Items": [_news(1)]}),
            (200, {"PagingInfo": {"HasMoreItems": True, "Bookmark": "b2"}, "Items": [_news(2)]}),
            (200, {"PagingInfo": {"HasMoreItems": False, "Bookmark": None}, "Items": [_news(3)]}),
        ]
    )
    items = LearnClient(t).get_announcements(ORG)
    assert [a.id for a in items] == ["1", "2", "3"]
    last = t.urls[-1]
    assert last.count("bookmark=") == 1 and last.endswith("bookmark=b2")


def test_repeated_page_raises_instead_of_returning_partial_data():
    same = f"{BASE_URL}{NEWS}?page=2"
    t = PagedTransport([(200, {"Objects": [_news(1)], "Next": same}), (200, {"Objects": [_news(2)], "Next": same})])
    with pytest.raises(LearnApiError, match="repeated"):
        LearnClient(t).get_announcements(ORG)


def test_page_limit_raises():
    pages = [(200, {"Objects": [_news(i)], "Next": f"{BASE_URL}{NEWS}?page={i + 2}"}) for i in range(MAX_PAGES + 1)]
    with pytest.raises(LearnApiError, match="exceeded"):
        LearnClient(PagedTransport(pages)).get_announcements(ORG)


def test_cross_host_next_link_is_refused():
    t = PagedTransport([(200, {"Objects": [_news(1)], "Next": "https://evil.example.com/x"})])
    with pytest.raises(LearnApiError, match="different host"):
        LearnClient(t).get_announcements(ORG)


def test_unexpected_collection_shape_raises():
    with pytest.raises(LearnApiError, match="unexpected"):
        LearnClient(PagedTransport([(200, {"Weird": 1})])).get_announcements(ORG)


def test_invalid_paging_info_raises():
    t = PagedTransport([(200, {"Items": [_news(1)], "PagingInfo": {"HasMoreItems": True}})])
    with pytest.raises(LearnApiError, match="bookmark"):
        LearnClient(t).get_announcements(ORG)


def test_403_on_a_later_page_is_empty_only_for_first_page():
    # A 403 on the first request means "tool unavailable" -> []; behaviour unchanged.
    client = client_with({"/news/": (403, {"Errors": [{"Message": "Not Authorized"}]})})
    assert client.get_announcements(ORG) == []


# ---- attachments ---------------------------------------------------------------------------


def test_assignment_attachments_parsed_leniently():
    folders = load_fixture("dropbox_folders.json")
    folders[0]["Attachments"] = [
        {"FileId": 11, "FileName": "spec.pdf", "Size": 2048},
        {"Id": 12, "Name": "data.csv"},
        {"Id": 13},  # no name -> skipped
        "junk",
    ]
    client = client_with({"dropbox/folders": (200, folders), "grades/values/myGradeValues": (200, [])})
    a = next(x for x in client.get_assignments(ORG) if x.id == "500001")
    assert [(x.id, x.name, x.size_bytes) for x in a.attachments] == [("11", "spec.pdf", 2048), ("12", "data.csv", None)]
    b = next(x for x in client.get_assignments(ORG) if x.id == "500002")
    assert b.attachments == []
