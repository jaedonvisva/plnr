"""Code-enforced course_id validation -- the fix for a real gap: course_id
was being passed straight into a Valence API URL path (see
learn/client.py's f-string URL construction) with NO validation anywhere
for six of the ten tools. Only search_announcements/search_course_content
happened to validate it (via agent/aggregation.py's _select_courses).

This is deliberately NOT prompt-enforced. The system prompt has always said
"course_id must be an ID returned by get_courses()" -- and Gemini/Qwen have
both been observed live inventing course IDs anyway (a plausible-looking
digit string, or a guessed "MSE232_20251"-style code). The system prompt is
guidance, not a security boundary; this module is the actual boundary.

Design (see module docstring notes below for the reasoning the PR asked
for -- refresh strategy, inactive courses, staleness):

- CourseRegistry is bound to one authenticated LearnClient, one per agent
  session (mirrors how build_tools() is already bound to one client).
- The known-ID set is populated lazily on first use and cached -- course
  enrollment does not change mid-conversation in the current (interactive,
  single-session) architecture, so refetching get_courses() before every
  single course-specific tool call would be pure overhead.
- BUT a long-running process (the future iMessage daemon) could plausibly
  see a real enrollment change (new term starts, a course is added) while
  it's running. So a validation MISS triggers exactly one refresh-and-recheck
  before finally rejecting -- staleness self-heals without ever having to
  trust an unverified ID.
- Inactive courses ARE included in the known set (unlike
  agent/aggregation.py's cross-course tools, which deliberately only
  search *active* courses by default). A course_id validation check is
  answering "is this a real course_id this student has ever had access
  to," not "is this course still current" -- get_grades()/get_assignments()
  on a past/inactive course is a completely legitimate, common request
  (e.g. "what was my grade in a course from last term").
- get_courses() itself always calls refresh() -- it's the model's own way
  of discovering current course_ids, so it must never serve a stale list.
"""

from learn.client import LearnClient
from learn.models import Course


class CourseRegistry:
    """Tracks which course_ids are real for one authenticated LearnClient."""

    def __init__(self, client: LearnClient):
        self._client = client
        self._known_ids: set[str] | None = None

    def refresh(self) -> list[Course]:
        """Fetch the current course list and reset the known-id cache from it."""
        courses = self._client.get_courses()
        self._known_ids = {c.id for c in courses}
        return courses

    def is_known(self, course_id: str) -> bool:
        """True if course_id belongs to a real course this account has
        access to. Self-heals one stale-cache miss by refreshing once
        before finally returning False."""
        if self._known_ids is None:
            self.refresh()
        if course_id in self._known_ids:
            return True
        self.refresh()
        return course_id in self._known_ids
