from datetime import datetime, timezone

from learn.models import Grade, parse_iso8601


def test_parse_iso8601_normal():
    dt = parse_iso8601("2026-05-16T03:59:59.000Z")
    assert dt == datetime(2026, 5, 16, 3, 59, 59, tzinfo=timezone.utc)
    assert dt.tzinfo is not None


def test_parse_iso8601_none_and_empty():
    assert parse_iso8601(None) is None
    assert parse_iso8601("") is None


def test_raw_field_excluded_from_repr_and_equality():
    common = dict(
        id="1",
        course_id="1",
        name="Test",
        points_earned=8.0,
        points_possible=10.0,
        percentage=80.0,
        displayed_grade="80 %",
        letter_grade=None,
        comments=None,
        graded_at=None,
    )
    a = Grade(**common, raw={"GradeObjectIdentifier": "1"})
    b = Grade(**common, raw={"something": "different"})

    assert a == b, "raw should not affect equality"
    assert "raw" not in repr(a)
