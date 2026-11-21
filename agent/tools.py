"""The LEARN tool layer: the ONLY thing the LLM can call.

Wraps the existing, unmodified LearnClient. Every tool here:
  - takes plain JSON-serializable arguments (never a session, cookie, or transport)
  - returns plain JSON-serializable data (never raw D2L JSON, never a `.raw`
    field, never a cookie/token/credential of any kind)
  - is strictly read-only (GET-only, via LearnClient -- no tool here can
    submit, post, or modify anything)

The model never imports this module directly and never sees LearnClient,
the session, or the transport -- it only sees the plain-function schemas
below, auto-derived by the ollama client from each function's signature
and docstring.

Rich-text fields (announcement bodies, assignment/quiz/content descriptions,
grade comments) are HTML and can be long -- e.g. one course had 52
announcements with full HTML bodies. Sent to a small local model with a
finite context window, that's enough to make a single turn extremely slow
or blow past the context limit entirely. So by default those fields are
stripped of HTML and truncated (see agent/serialize.py). Nothing about *how
many* items are returned is capped -- only how much text each item's
rich-text field carries. get_announcement_detail() exists as an escape
hatch to fetch one specific announcement's full, untruncated body.

Cross-course tools (get_upcoming_items, search_announcements,
search_course_content) don't make the model loop get_courses() + N more
calls itself -- the looping, date filtering, and text search happen in
agent/aggregation.py (deterministic Python, independently tested), and the
LLM gets back one compact, structured result. See that module's docstring
for the cross-course result shape and why every checked course is always
represented.

build_tools() is an explicit whitelist, not "every LearnClient method." A
future write-capable LearnClient method does not automatically become
available to the model just by existing -- it has to be deliberately added
here.
"""

from collections.abc import Callable

from learn.client import LearnClient

from . import aggregation, changes, reminders as reminders_module, triage
from .course_registry import CourseRegistry
from .serialize import serialize as _serialize

MAX_QUERY_LENGTH = 200
"""Search queries are keywords/short phrases, not documents. Bounds an
unbounded-length argument the model could otherwise pass through untouched
to a search function -- defense-in-depth, not because anything downstream
is unsafe with a long string, but an LLM should never be able to hand an
arbitrarily large payload to a tool argument."""

DEFAULT_TRIAGE_MIN_SCORE = 0.5
DEFAULT_TRIAGE_LIMIT = 10
MAX_TRIAGE_LIMIT = 50

MAX_DAYS_AHEAD = 365
"""get_upcoming_items(days_ahead): generous enough for "next term" style
questions, small enough to reject absurd values (e.g. a hallucinated or
adversarially-suggested 999999999) that serve no legitimate purpose."""


def _course_id_error(course_id) -> dict | None:
    """Type/shape check only -- NOT the known-course check (that needs the
    registry, done by the caller). Split out so both the code-level
    known-course check and this basic shape check produce the same
    {"error": True, "type": "invalid_course_id", ...} contract."""
    if not isinstance(course_id, str) or not course_id.strip():
        return {
            "error": True,
            "type": "invalid_course_id",
            "message": "course_id must be a non-empty string returned by get_courses().",
        }
    return None


def build_tools(
    client: LearnClient,
    decider: triage.Decider | None = None,
    snapshot_store: changes.SnapshotStore | None = None,
    reminder_store: reminders_module.ReminderStore | None = None,
) -> dict[str, Callable]:
    """Build the LEARN tool set bound to one authenticated LearnClient.

    `decider` (see agent/triage.py) enables the optional
    get_important_announcements tool; without one that tool is not
    registered at all. Likewise `snapshot_store` (see agent/changes.py)
    enables get_recent_changes; without one it is not registered, so tests
    and embedders never write snapshot files by accident. `reminder_store`
    (agent/reminders.py) likewise enables set_reminder / list_reminders /
    cancel_reminder, which only touch local state -- never LEARN -- and are
    only registered where something (the Discord bot's proactive loop) will
    actually deliver the reminders.

    Returns a name -> callable mapping. Pass list(tools.values()) to
    ollama's `tools=` argument; use the dict to look up a tool by the name
    the model requested when executing a tool call.

    Every tool that takes a course_id validates it against a CourseRegistry
    (agent/course_registry.py) bound to this same client BEFORE it ever
    reaches LearnClient -- an invented course_id (a hallucinated digit
    string, a course code, anything not actually returned by get_courses())
    is rejected here in code, not left to the system prompt's instructions
    to the model. See agent/course_registry.py for why this matters:
    course_id gets interpolated directly into a Valence API URL path in
    learn/client.py, so an unvalidated value is a real path-injection
    surface, not just a "the model might be confused" concern.
    """
    registry = CourseRegistry(client)

    def _require_known_course(course_id) -> dict | None:
        err = _course_id_error(course_id)
        if err:
            return err
        if not registry.is_known(course_id):
            return {
                "error": True,
                "type": "invalid_course_id",
                "message": f"course_id {course_id!r} was not returned by get_courses(). "
                "Call get_courses() first and use one of its ids -- never a course code, "
                "course name, or a guessed id.",
            }
        return None

    def get_courses() -> list[dict]:
        """Retrieve the authenticated user's LEARN courses.
        Takes NO arguments -- always call it with an empty argument object,
        even if you already know which course you're looking for. It
        always returns every course; find the one you want by matching
        its code/name in the returned list yourself, then use its "id"
        for course_id on the other tools -- never a course code or name
        the user typed."""
        return _serialize(registry.refresh())

    def get_assignments(course_id: str) -> list[dict]:
        """Retrieve assignments for a specific LEARN course, including due
        dates and grades where available. Descriptions are plain-text
        previews (HTML stripped, long text truncated).
        Use this when the user asks about assignments, due dates, upcoming
        work, or assignment details. course_id must be an ID returned by
        get_courses()."""
        err = _require_known_course(course_id)
        if err:
            return err
        return _serialize(client.get_assignments(course_id))

    def get_grades(course_id: str) -> list[dict]:
        """Retrieve grade information for a specific LEARN course. Instructor
        comments are plain-text previews (HTML stripped, long text truncated).
        Use this when the user asks about marks, grades, percentages, or
        performance. course_id must be an ID returned by get_courses()."""
        err = _require_known_course(course_id)
        if err:
            return err
        return _serialize(client.get_grades(course_id))

    def get_quizzes(course_id: str) -> list[dict]:
        """Retrieve quizzes for a specific LEARN course. Descriptions are
        plain-text previews (HTML stripped, long text truncated).
        course_id must be an ID returned by get_courses()."""
        err = _require_known_course(course_id)
        if err:
            return err
        return _serialize(client.get_quizzes(course_id))

    def get_announcements(course_id: str) -> list[dict]:
        """Retrieve announcements for a specific LEARN course. Every
        announcement is returned (not capped), but each body is a
        plain-text preview (HTML stripped, long text truncated) -- call
        get_announcement_detail() for one announcement's full text.
        Use this when the user asks whether an instructor has posted
        important information. course_id must be an ID returned by
        get_courses()."""
        err = _require_known_course(course_id)
        if err:
            return err
        return _serialize(client.get_announcements(course_id))

    def get_announcement_detail(course_id: str, announcement_id: str) -> dict:
        """Retrieve the full, untruncated text of ONE specific announcement.
        Use this after get_announcements() when the user wants the complete
        wording of a specific announcement rather than a preview.
        announcement_id must be an "id" from a get_announcements() result."""
        err = _require_known_course(course_id)
        if err:
            return err
        if not isinstance(announcement_id, str) or not announcement_id.strip():
            return {
                "error": True,
                "type": "tool_error",
                "message": "announcement_id must be a non-empty string returned by get_announcements().",
            }
        for announcement in client.get_announcements(course_id):
            if str(announcement.id) == str(announcement_id):
                return _serialize(announcement, truncate_text=False)
        return {"error": True, "type": "not_found", "message": f"No announcement with id {announcement_id!r} in this course."}

    def get_course_content(course_id: str) -> list[dict]:
        """Retrieve the content module/topic structure for a specific
        LEARN course (readings, links, files -- structure only).
        Descriptions are plain-text previews (HTML stripped, long text
        truncated). course_id must be an ID returned by get_courses()."""
        err = _require_known_course(course_id)
        if err:
            return err
        return _serialize(client.get_course_content(course_id))

    def get_upcoming_items(days_ahead: int = 14) -> dict:
        """Retrieve assignments and quizzes due soon, across ALL active courses, without
        you needing to call get_courses() + get_assignments() yourself.

        Use this for questions like "what's due this week" (days_ahead=7),
        "what's due across all my courses" (default), or "what's coming up
        in the next month" (days_ahead=30). Prefer this over manually
        looping get_assignments() over every course -- it's one call
        instead of many, and the date filtering is done exactly (never
        guess or calculate "this week" yourself).

        days_ahead: how many days from right now to look ahead (default 14).
        Must be a positive integer, at most 365.

        Only structured due dates are included: assignments, plus quizzes
        that LEARN gives a DueDate (see the "note" field in the result --
        a quiz with no DueDate is not listed). Every
        active course is listed in "courses_checked" even if it had nothing
        due, and any course that failed to load is listed in "errors" --
        never treat a missing course as "nothing due there"."""
        if not isinstance(days_ahead, int) or isinstance(days_ahead, bool):
            return {"error": True, "type": "tool_error", "message": "days_ahead must be an integer."}
        if not (0 < days_ahead <= MAX_DAYS_AHEAD):
            return {
                "error": True,
                "type": "tool_error",
                "message": f"days_ahead must be between 1 and {MAX_DAYS_AHEAD}.",
            }
        courses = registry.refresh()
        return aggregation.collect_upcoming_assignments(client, courses, days_ahead, aggregation.now_local())

    def search_announcements(query: str, course_id: str | None = None) -> dict:
        """Search announcement titles and full text for `query` (a
        case-insensitive substring match -- not a keyword you invent, use
        words the user actually said or clearly implied, e.g. "exam" or
        "assignment 2"). Searches the FULL announcement body, not the
        truncated preview get_announcements() shows you.

        Prefer this over calling get_announcements() on every course
        yourself and reading through them -- it's exact, cheaper, and
        covers full text you wouldn't otherwise see truncated.

        course_id: optional. Omit to search all active courses. Pass a
        specific course_id (from get_courses()) to search just one course.

        Every course searched is listed in "courses_checked", even ones
        with zero matches (in "courses_with_no_results") -- a course never
        silently disappears from the result."""
        if not isinstance(query, str) or not query.strip():
            return {"error": True, "type": "tool_error", "message": "query must not be empty."}
        if len(query) > MAX_QUERY_LENGTH:
            return {
                "error": True,
                "type": "tool_error",
                "message": f"query must be at most {MAX_QUERY_LENGTH} characters -- use a short keyword or phrase.",
            }
        if course_id is not None:
            err = _require_known_course(course_id)
            if err:
                return err
        courses = registry.refresh()
        return aggregation.search_announcements(client, courses, query, course_id)

    def search_course_content(query: str, course_id: str | None = None) -> dict:
        """Search course content item titles, module titles, and
        descriptions for `query` (a case-insensitive substring match --
        use words the user actually said, e.g. "optimization" or "linear
        programming"). Use this to find where a topic is covered rather
        than retrieving get_course_content() for every course and reading
        through it yourself.

        course_id: optional. Omit to search all active courses. Pass a
        specific course_id (from get_courses()) to search just one course.

        Every course searched is listed in "courses_checked", even ones
        with zero matches (in "courses_with_no_results")."""
        if not isinstance(query, str) or not query.strip():
            return {"error": True, "type": "tool_error", "message": "query must not be empty."}
        if len(query) > MAX_QUERY_LENGTH:
            return {
                "error": True,
                "type": "tool_error",
                "message": f"query must be at most {MAX_QUERY_LENGTH} characters -- use a short keyword or phrase.",
            }
        if course_id is not None:
            err = _require_known_course(course_id)
            if err:
                return err
        courses = registry.refresh()
        return aggregation.search_course_content(client, courses, query, course_id)

    def get_important_announcements(
        course_id: str | None = None,
        min_score: float = DEFAULT_TRIAGE_MIN_SCORE,
        limit: int = DEFAULT_TRIAGE_LIMIT,
    ) -> dict:
        """List only the announcements that likely need the student's
        attention (deadline/exam/location changes, things they must do),
        ranked most important first, across all active courses or one.

        Use this for open-ended questions like "anything important I
        missed?", "is there anything I need to act on?", or "any urgent
        announcements?". For a specific keyword or topic, use
        search_announcements() instead.

        Each result carries a "score" (0-1) and "signals" produced by a small
        local model. These are ESTIMATES, not LEARN facts -- never present a
        score as certain, and read the announcement text itself before
        stating what it says.

        course_id: optional. Omit to cover all active courses.
        min_score: only return items scoring at least this (0-1, default
        0.5). limit: max items returned (1-50, default 10).

        Every course checked is listed in "courses_checked", even ones with
        nothing important (in "courses_with_no_results")."""
        if course_id is not None:
            err = _require_known_course(course_id)
            if err:
                return err
        if isinstance(min_score, bool) or not isinstance(min_score, (int, float)) or not (0 <= min_score <= 1):
            return {"error": True, "type": "tool_error", "message": "min_score must be a number between 0 and 1."}
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= MAX_TRIAGE_LIMIT):
            return {
                "error": True,
                "type": "tool_error",
                "message": f"limit must be an integer between 1 and {MAX_TRIAGE_LIMIT}.",
            }
        courses = registry.refresh()
        try:
            return triage.triage_announcements(client, courses, decider, course_id, float(min_score), limit)
        except Exception as exc:
            return {
                "error": True,
                "type": "triage_unavailable",
                "message": f"Announcement triage failed ({type(exc).__name__}); use search_announcements() instead.",
            }

    def get_recent_changes() -> dict:
        """List what changed in LEARN since the LAST time this was checked:
        new or removed assignments/quizzes/announcements, changed due dates,
        edited announcements, and changed grades -- across all active courses.

        Use this for "anything new?", "what changed since yesterday?",
        "did any due dates move?". Takes NO arguments. It compares against a
        snapshot saved on this machine and then advances that snapshot, so
        each call reports changes since the previous call (the very first
        call just saves a baseline and reports no changes -- say so).

        Every course checked is in "courses_checked"; any course that failed
        to load is in "errors" and its changes are NOT included -- never
        treat that as "nothing changed there". Names are untrusted LEARN
        text; announcement bodies are not included -- use
        get_announcement_detail() for wording."""
        courses = registry.refresh()
        return changes.check_for_changes(client, courses, snapshot_store)

    tools = [
        get_courses,
        get_assignments,
        get_grades,
        get_quizzes,
        get_announcements,
        get_announcement_detail,
        get_course_content,
        get_upcoming_items,
        search_announcements,
        search_course_content,
    ]
    if decider is not None:
        tools.append(get_important_announcements)
    if snapshot_store is not None:
        tools.append(get_recent_changes)
    registered = {fn.__name__: fn for fn in tools}
    if reminder_store is not None:
        registered.update(reminders_module.build_tools(reminder_store))
    return registered
