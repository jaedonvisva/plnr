"""Read-only LEARN data client -- the normalization boundary.

All methods here call Brightspace's *documented* Valence REST API
(`/d2l/api/lp/...` and `/d2l/api/le/...`), authenticated by the same
session cookies the user's own browser login established (via Playwright's
`context.request`, which shares the context's cookie jar automatically).
See learn/browser.py and learn/auth.py for that flow -- unchanged here.

No write operations are implemented or attempted. No HTML scraping is used --
every endpoint below was verified live to return structured JSON.

Every public get_* method returns domain models from learn/models.py, not
raw D2L JSON -- callers (including the future agent) never need to know
these endpoints exist. Raw JSON is still available per-object via `.raw`
for anything not represented in the model.

Error handling: see learn/exceptions.py. A 401 means "your session expired,
re-authenticate" and is raised as LearnAuthenticationError -- it is never
silently turned into an empty list. A 403 ("Not Authorized" for a specific
tool/course) is raised internally as LearnToolUnavailableError and *is*
turned into an empty list by the get_* methods below, because "this course
doesn't have Grades enabled" is a legitimate business meaning of "no data",
distinct from "you're not logged in". Any other non-2xx response raises
LearnApiError.
"""

from urllib.parse import urlencode

from playwright.sync_api import BrowserContext

from .exceptions import LearnApiError, LearnAuthenticationError, LearnToolUnavailableError
from .models import Announcement, Assignment, ContentItem, Course, Grade, Quiz, parse_iso8601

BASE_URL = "https://learn.uwaterloo.ca"
COURSE_OFFERING_TYPE_ID = 3


class LearnClient:
    """Read-only wrapper around LEARN's authenticated Valence REST API."""

    def __init__(self, context: BrowserContext):
        self._request = context.request
        self._le_version = None
        self._lp_version = None

    def _versions(self) -> tuple[str, str]:
        """Discover the current 'le' and 'lp' API versions this LEARN instance supports.

        Queried once and cached, rather than hardcoding a version number that
        may go stale if Waterloo upgrades Brightspace.
        """
        if self._le_version is None or self._lp_version is None:
            products = self._get(f"{BASE_URL}/d2l/api/versions/")
            by_code = {p["ProductCode"]: p["LatestVersion"] for p in products}
            self._le_version = by_code["le"]
            self._lp_version = by_code["lp"]
        return self._le_version, self._lp_version

    def _get(self, url: str):
        """GET a Valence endpoint, raising a typed exception on failure.

        See module docstring / learn/exceptions.py for what each status
        means and why. Defensively also checks the response body shape,
        in case some endpoint ever returns 200 with an {"Errors": [...]}
        body instead of a non-2xx status -- both paths raise the same
        exception type either way.
        """
        response = self._request.get(url)
        if response.status == 401:
            raise LearnAuthenticationError(
                "LEARN session is missing or expired -- re-authenticate.", status=401, url=url
            )
        if response.status == 403:
            raise LearnToolUnavailableError(
                "Not authorized for this resource (tool disabled for this course, "
                "or a permission error -- Valence doesn't distinguish the two).",
                status=403,
                url=url,
            )
        if not response.ok:
            raise LearnApiError(f"Unexpected response {response.status} from LEARN.", status=response.status, url=url)

        data = response.json()
        if isinstance(data, dict) and "Errors" in data:
            raise LearnToolUnavailableError(str(data["Errors"]), status=response.status, url=url)
        return data

    def _get_or_empty(self, url: str):
        """Like _get(), but treats LearnToolUnavailableError as "no data for
        this course" and returns None instead of raising. Authentication and
        other API errors still propagate."""
        try:
            return self._get(url)
        except LearnToolUnavailableError:
            return None

    def get_courses(self) -> list[Course]:
        """Return the authenticated user's active course offerings."""
        _, lp_version = self._versions()
        courses = []
        bookmark = None
        while True:
            url = f"{BASE_URL}/d2l/api/lp/{lp_version}/enrollments/myenrollments/"
            if bookmark:
                url += f"?{urlencode({'bookmark': bookmark})}"
            data = self._get(url)
            for item in data["Items"]:
                org_unit = item["OrgUnit"]
                if org_unit["Type"]["Id"] != COURSE_OFFERING_TYPE_ID:
                    continue
                access = item["Access"]
                courses.append(
                    Course(
                        id=str(org_unit["Id"]),
                        name=org_unit["Name"],
                        code=org_unit.get("Code"),
                        is_active=access["IsActive"],
                        url=org_unit["HomeUrl"],
                        last_accessed_at=parse_iso8601(access.get("LastAccessed")),
                        raw=item,
                    )
                )
            paging = data["PagingInfo"]
            if not paging["HasMoreItems"]:
                break
            bookmark = paging["Bookmark"]
        return courses

    def get_assignments(self, course_id: str) -> list[Assignment]:
        """Return Dropbox assignments for a course, with grades joined in
        from the Grades endpoint where available.

        Returns an empty list if this course doesn't have the Dropbox tool
        enabled.
        """
        le_version, _ = self._versions()
        folders = self._get_or_empty(f"{BASE_URL}/d2l/api/le/{le_version}/{course_id}/dropbox/folders/")
        if not folders:
            return []

        grades_by_item_id = {g.id: g for g in self.get_grades(course_id)}

        assignments = []
        for f in folders:
            grade_item_id = f.get("GradeItemId")
            grade = grades_by_item_id.get(str(grade_item_id)) if grade_item_id is not None else None
            assessment = f.get("Assessment") or {}
            assignments.append(
                Assignment(
                    id=str(f["Id"]),
                    course_id=str(course_id),
                    name=f["Name"],
                    description=(f.get("CustomInstructions") or {}).get("Html") or None,
                    due_at=parse_iso8601(f.get("DueDate")),
                    max_points=assessment.get("ScoreDenominator"),
                    grade_item_id=str(grade_item_id) if grade_item_id is not None else None,
                    points_earned=grade.points_earned if grade else None,
                    points_possible=grade.points_possible if grade else None,
                    is_hidden=f.get("IsHidden", False),
                    raw=f,
                )
            )
        return assignments

    def get_grades(self, course_id: str) -> list[Grade]:
        """Return the authenticated user's own grade values for a course.

        Returns an empty list if this course doesn't have Grades enabled.
        """
        le_version, _ = self._versions()
        values = self._get_or_empty(f"{BASE_URL}/d2l/api/le/{le_version}/{course_id}/grades/values/myGradeValues/")
        if not values:
            return []
        return [self._to_grade(v, course_id) for v in values]

    def _to_grade(self, v: dict, course_id: str) -> Grade:
        displayed = v.get("DisplayedGrade", "")
        percentage = None
        if isinstance(displayed, str) and displayed.strip().endswith("%"):
            try:
                percentage = float(displayed.strip().rstrip("%").strip())
            except ValueError:
                percentage = None
        return Grade(
            id=str(v["GradeObjectIdentifier"]),
            course_id=str(course_id),
            name=v["GradeObjectName"],
            points_earned=v.get("PointsNumerator"),
            points_possible=v.get("PointsDenominator"),
            percentage=percentage,
            displayed_grade=displayed,
            letter_grade=None,
            comments=(v.get("Comments") or {}).get("Html") or None,
            graded_at=parse_iso8601(v.get("LastModified")),
            raw=v,
        )

    def get_quizzes(self, course_id: str) -> list[Quiz]:
        """Return quiz definitions for a course (not per-attempt scores).

        Returns an empty list if this course doesn't have Quizzes enabled.
        """
        le_version, _ = self._versions()
        data = self._get_or_empty(f"{BASE_URL}/d2l/api/le/{le_version}/{course_id}/quizzes/")
        if not data:
            return []
        quizzes = data["Objects"] if isinstance(data, dict) else data
        return [
            Quiz(
                id=str(q["QuizId"]),
                course_id=str(course_id),
                name=q["Name"],
                is_active=q["IsActive"],
                grade_item_id=str(q["GradeItemId"]) if q.get("GradeItemId") is not None else None,
                description=(q.get("Description") or {}).get("Text", {}).get("Html") or None,
                raw=q,
            )
            for q in quizzes
        ]

    def get_announcements(self, course_id: str) -> list[Announcement]:
        """Return news/announcements posted in a course.

        Returns an empty list if this course doesn't have News enabled.
        """
        le_version, _ = self._versions()
        data = self._get_or_empty(f"{BASE_URL}/d2l/api/le/{le_version}/{course_id}/news/")
        if not data:
            return []
        return [
            Announcement(
                id=str(n["Id"]),
                course_id=str(course_id),
                title=n["Title"],
                body=(n.get("Body") or {}).get("Html") or None,
                is_hidden=n.get("IsHidden", False),
                created_at=parse_iso8601(n.get("CreatedDate")),
                updated_at=parse_iso8601(n.get("LastModifiedDate")),
                raw=n,
            )
            for n in data
        ]

    def get_course_content(self, course_id: str) -> list[ContentItem]:
        """Return the course's content items, flattened from the module tree.

        Returns an empty list if this course doesn't have Content enabled.
        """
        le_version, _ = self._versions()
        data = self._get_or_empty(f"{BASE_URL}/d2l/api/le/{le_version}/{course_id}/content/toc")
        if not data:
            return []
        items: list[ContentItem] = []
        for module in data.get("Modules", []):
            self._walk_content_module(module, course_id, items)
        return items

    def _walk_content_module(self, module: dict, course_id: str, items: list[ContentItem]) -> None:
        for topic in module.get("Topics", []):
            items.append(
                ContentItem(
                    id=str(topic["TopicId"]),
                    course_id=str(course_id),
                    module_title=module["Title"],
                    title=topic["Title"],
                    type=topic["TypeIdentifier"],
                    url=topic.get("Url") or None,
                    description=(topic.get("Description") or {}).get("Html") or None,
                    grade_item_id=str(topic["GradeItemId"]) if topic.get("GradeItemId") is not None else None,
                    updated_at=parse_iso8601(topic.get("LastModifiedDate")),
                    raw=topic,
                )
            )
        for submodule in module.get("Modules", []):
            self._walk_content_module(submodule, course_id, items)

    def get_discussion_forums(self, course_id: str) -> list[dict]:
        """Return discussion forums for a course (forum-level only, not topics/posts).

        Not normalized into a domain model -- no Forum/Discussion model was
        requested for this milestone. Still routed through the same error
        handling as everything else (see module docstring), so an expired
        session raises rather than silently returning [].

        Returns an empty list if this course doesn't have Discussions enabled.
        """
        le_version, _ = self._versions()
        data = self._get_or_empty(f"{BASE_URL}/d2l/api/le/{le_version}/{course_id}/discussions/forums/")
        if not data:
            return []
        return [
            {
                "id": f["ForumId"],
                "name": f["Name"],
                "description_html": (f.get("Description") or {}).get("Html") or None,
                "is_locked": f["IsLocked"],
                "is_hidden": f["IsHidden"],
            }
            for f in data
        ]
