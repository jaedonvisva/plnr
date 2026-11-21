"""Domain models: the boundary between raw Valence JSON and everything else.

Every field here was chosen because it was observed in a real, live Valence
response during development (see learn/client.py for the endpoints). Nothing
below is guessed from D2L documentation we haven't verified against this
LEARN instance.

All datetimes are timezone-aware and in UTC. D2L returns ISO-8601 timestamps
already suffixed with "Z" (UTC) -- parse_iso8601() below parses them as-is
and attaches UTC tzinfo; nothing is converted to local time, and no
timezone information is ever silently discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def parse_iso8601(value: str | None) -> datetime | None:
    """Parse a D2L ISO-8601 UTC timestamp (e.g. "2026-05-16T03:59:59.000Z").

    Returns a timezone-aware datetime in UTC, or None if value is None/empty.
    """
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _html_or_none(text_block: dict[str, Any] | None) -> str | None:
    """D2L rich-text fields look like {"Text": "...", "Html": "..."}. Many are
    present-but-empty (""); normalize "no content" to None rather than ""."""
    if not text_block:
        return None
    html = text_block.get("Html")
    return html or None


@dataclass(frozen=True)
class Course:
    """A course offering the authenticated user is enrolled in.

    Source: GET /d2l/api/lp/{lp}/enrollments/myenrollments/ (Items[].OrgUnit / .Access)
    """

    id: str
    name: str
    code: str | None
    """OrgUnit.Code -- the offering's section code as LEARN stores it
    (e.g. "MSE232_prof_1265"), NOT a prettified "MSE 232" display code.
    The enrollments endpoint does not provide a prettified code, and we do
    not infer one from the name."""
    is_active: bool
    url: str
    """OrgUnit.HomeUrl -- a real, directly-provided navigable URL."""
    last_accessed_at: datetime | None
    """Access.LastAccessed, UTC."""
    display_code: str | None = None
    """"MSE 232", inferred from the start of `code` (see learn/terms.py). Not a
    LEARN field; None when `code` doesn't start like a course code."""
    term: str | None = None
    """Waterloo term code (e.g. "1265") inferred from `code`."""
    term_label: str | None = None
    """"Spring 2026", derived from `term`."""
    raw: dict[str, Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Attachment:
    """A file attached to an assignment's instructions (metadata only, never downloaded).

    Source: Attachments[] on a dropbox folder. Field names (Id/FileId, FileName/Name,
    Size) follow agent2learn's handling; not yet confirmed on a live LEARN response here.
    """

    id: str
    name: str
    size_bytes: int | None


@dataclass(frozen=True)
class Assignment:
    """A Dropbox (file-submission) assignment.

    Source: GET /d2l/api/le/{le}/{course_id}/dropbox/folders/
    Grade fields (points_earned/points_possible), when present, are joined
    in from the Grades endpoint by LearnClient -- not from this endpoint
    alone (Dropbox folders do not include the student's grade).
    """

    id: str
    course_id: str
    name: str
    description: str | None
    """CustomInstructions.Html, or None if empty."""
    due_at: datetime | None
    max_points: float | None
    """Assessment.ScoreDenominator."""
    grade_item_id: str | None
    points_earned: float | None
    """Joined from Grade.points_earned via grade_item_id. None if ungraded
    or if this course's Grades tool isn't reachable."""
    points_possible: float | None
    """Joined from Grade.points_possible via grade_item_id."""
    is_hidden: bool
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    # Deliberately NOT included: submission_status, url.
    # - Dropbox folders expose TotalUsersWithSubmissions etc., but these were
    #   observed as -1 (unpopulated) for the Student role -- not reliable.
    # - No per-assignment browsable URL field was found on this resource
    #   (ActivityId is an opaque LTI-style identifier, not a page link).


@dataclass(frozen=True)
class Grade:
    """The authenticated user's own grade value for one grade item.

    Source: GET /d2l/api/le/{le}/{course_id}/grades/values/myGradeValues/
    """

    id: str
    """GradeObjectIdentifier."""
    course_id: str
    name: str
    """GradeObjectName."""
    points_earned: float | None
    """PointsNumerator."""
    points_possible: float | None
    """PointsDenominator."""
    percentage: float | None
    """Parsed from DisplayedGrade only when it's a "N %" string (the only
    shape observed on this account, GradeObjectTypeName == "Numeric").
    Not computed by us from points_earned/points_possible -- taken from
    D2L's own authoritative displayed value."""
    displayed_grade: str
    """DisplayedGrade, verbatim (e.g. "51.18 %", "100 %"). The authoritative
    source of truth; use this over `percentage` if in doubt."""
    letter_grade: str | None
    """None on every grade item observed so far (this account only has
    Numeric-type grade items). Field kept because GradeObjectTypeName is a
    documented Valence enum that includes non-numeric schemes; populate this
    if/when a non-numeric GradeObjectTypeName is actually seen."""
    comments: str | None
    """Comments.Html (instructor feedback), or None if empty."""
    graded_at: datetime | None
    """LastModified, UTC."""
    raw: dict[str, Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Quiz:
    """A quiz definition.

    Source: GET /d2l/api/le/{le}/{course_id}/quizzes/
    """

    id: str
    course_id: str
    name: str
    is_active: bool
    grade_item_id: str | None
    description: str | None
    """Description.Text.Html, or None if empty."""
    due_at: datetime | None = None
    """DueDate, UTC. Read the way agent2learn does; not yet confirmed on a live
    LEARN quiz response here, and often null (quiz availability is usually
    expressed via start/end instead)."""
    start_at: datetime | None = None
    """StartDate, UTC."""
    end_at: datetime | None = None
    """EndDate, UTC."""
    raw: dict[str, Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Announcement:
    """A news/announcement post in a course.

    Source: GET /d2l/api/le/{le}/{course_id}/news/
    """

    id: str
    course_id: str
    title: str
    body: str | None
    """Body.Html, or None if empty."""
    is_hidden: bool
    created_at: datetime | None
    updated_at: datetime | None
    raw: dict[str, Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ContentItem:
    """One topic (leaf item) from a course's content tree.

    Source: GET /d2l/api/le/{le}/{course_id}/content/toc (Modules[].Topics[],
    walked recursively; the tree is flattened -- module_title records the
    immediate parent module's title, nested submodule structure is not
    preserved).
    """

    id: str
    """TopicId."""
    course_id: str
    module_title: str
    title: str
    type: str
    """TypeIdentifier, e.g. "Link". Passed through as D2L provides it --
    the full set of possible values was not enumerated against this
    account's data, so no enum/validation is applied."""
    url: str | None
    description: str | None
    """Description.Html, or None if empty."""
    grade_item_id: str | None
    updated_at: datetime | None
    """LastModifiedDate, UTC."""
    raw: dict[str, Any] | None = field(default=None, repr=False, compare=False)
