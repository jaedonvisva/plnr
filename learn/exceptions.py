"""Exceptions for LearnClient, distinguishing why a request didn't return data.

Without this, every non-2xx or error-shaped Valence response looked identical
to "this course legitimately has zero items" -- including an expired session,
which would make a future agent believe the user has no assignments at all.
"""


class LearnApiError(Exception):
    """Base class for any Valence API failure. Also raised directly for
    unexpected/unclassified errors (case 5: "other API error")."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class LearnAuthenticationError(LearnApiError):
    """The session is missing or expired (HTTP 401). The caller should
    re-authenticate (`python agent.py auth`) rather than treat
    this as "no data"."""


class LearnToolUnavailableError(LearnApiError):
    """Valence returned "Not Authorized" (HTTP 403) for this specific
    org unit/tool. Observed in practice for non-credit orientation modules
    that don't have Dropbox/Grades/Content/etc. enabled.

    Note: Valence does not give us enough information to reliably tell
    "tool disabled for this course" apart from "you don't have permission
    for this resource" -- both surface as the same 403 + {"Errors": [...]}
    shape in every case observed so far. This exception covers both; see
    the client's module docstring for details.
    """


class LoginError(Exception):
    """Interactive sign-in (browser/CDP or cookie paste) did not produce a verified session.
    Distinct from LearnAuthenticationError, which is an API call finding the session expired."""


class BrowserNotFound(LoginError):
    """No Chrome/Edge/Chromium executable could be located for dedicated-browser sign-in."""
