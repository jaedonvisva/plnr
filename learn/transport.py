"""Plain-HTTP transport for the Valence API, driven by a stored LEARN session.

Replays the stored LEARN cookies over plain HTTPS. LearnClient only needs `.get(url)` returning
an object with `.status`, `.ok` and `.json()`, and this provides exactly that surface.

Behavior ported from agent2learn (https://github.com/ManagementMO/agent2learn,
Apache-2.0, src/agent2learn/api.py):
  - same-origin only; no proxy/netrc/env trust; redirects are not followed
  - an HTML response (or a redirect) where JSON was expected means the session expired
    -> LearnAuthenticationError, never an empty result
  - idempotent GETs retry on 429/5xx with exponential backoff and Retry-After

Beyond agent2learn: an optional `reauth` callback. When a request finds the session
expired, the transport calls it once to obtain a fresh Session (agent.py passes
`auth.authenticate`, which opens the dedicated browser), swaps in the new cookies and
retries that request a single time. A failed re-auth is not retried for
REAUTH_COOLDOWN_SECONDS so one turn can't spawn a pile of browser windows.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from .exceptions import LearnAuthenticationError, LoginError
from .session import Session

USER_AGENT = "plnr/0.1 (+https://github.com/jaedonvisva/plnr)"
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.0
MAX_RETRY_AFTER = 60.0
THROTTLE_SECONDS = 0.05
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
REAUTH_COOLDOWN_SECONDS = 120.0


class Response:
    """The tiny response surface LearnClient reads (`.status`, `.ok`, `.json()`)."""

    def __init__(self, status: int, body: bytes, url: str):
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body
        self._url = url

    def json(self) -> Any:
        import json

        return json.loads(self._body)


class SessionTransport:
    def __init__(
        self,
        session: Session,
        *,
        reauth: Callable[[], Session] | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._reauth = reauth
        self._sleep = sleep
        self._clock = clock
        self._reauth_failed_at: float | None = None
        self._apply(session)

    def _apply(self, session: Session) -> None:
        self._base = urlsplit(session.base_url)
        self._http = requests.Session()
        # Authenticated requests must not inherit ambient proxy, netrc, or certificate
        # settings; a caller that wants a proxy must configure an explicit transport.
        self._http.trust_env = False
        self._http.cookies.update(session.requests_cookies())
        self._http.headers["User-Agent"] = USER_AGENT
        if session.xsrf:
            self._http.headers["X-Csrf-Token"] = session.xsrf

    def replace_session(self, session: Session) -> None:
        """Swap in fresh cookies (e.g. after `agent.py auth` ran in another process)."""
        self._apply(session)
        self._reauth_failed_at = None

    def get(self, url: str) -> Response:
        try:
            return self._get_once(url)
        except LearnAuthenticationError as expired:
            if self._reauth is None:
                raise
            if (
                self._reauth_failed_at is not None
                and self._clock() - self._reauth_failed_at < REAUTH_COOLDOWN_SECONDS
            ):
                raise
            print("\nLEARN session expired -- signing in again (complete the login in the browser window)...")
            try:
                fresh = self._reauth()
            except LoginError as exc:
                self._reauth_failed_at = self._clock()
                raise LearnAuthenticationError(
                    f"LEARN session expired and re-authentication failed: {exc}",
                    status=expired.status,
                    url=url,
                ) from exc
            self._reauth_failed_at = None
            self._apply(fresh)
            # One retry only: if the brand-new session is also rejected, surface it.
            return self._get_once(url)

    def _get_once(self, url: str) -> Response:
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.hostname, parsed.port or 443) != (
            self._base.scheme,
            self._base.hostname,
            self._base.port or 443,
        ):
            raise ValueError("refusing to send LEARN session cookies to a different origin")

        delay = BACKOFF_BASE
        for attempt in range(1, MAX_ATTEMPTS + 1):
            raw = self._http.get(
                url,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=False,
            )
            try:
                status = raw.status_code
                content_type = raw.headers.get("Content-Type", "").casefold()
                if status == 401 or 300 <= status < 400 or "text/html" in content_type:
                    # Valence answers JSON. A 401, redirect, or HTML page here is the login page.
                    raise LearnAuthenticationError(
                        "LEARN session is missing or expired -- run: python agent.py auth",
                        status=status,
                        url=url,
                    )
                if status in _RETRYABLE and attempt < MAX_ATTEMPTS:
                    wait = min(_retry_after(raw.headers.get("Retry-After")) or delay, MAX_RETRY_AFTER)
                    delay = min(delay * 2, MAX_RETRY_AFTER)
                else:
                    body = raw.content
                    self._sleep(THROTTLE_SECONDS)
                    return Response(status, body, url)
            finally:
                raw.close()
            self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover


def _retry_after(value: str | None) -> float | None:
    """Parse Retry-After as delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
