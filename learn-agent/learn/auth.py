"""Authentication detection and diagnostics.

This module never reads, stores, or transmits credentials. It only inspects
public page state (URL, title, DOM, cookie names/domains, storage key names)
to decide whether the user has completed Waterloo's WatIAM + Duo login flow
in the visible browser themselves.
"""

import time
from urllib.parse import urlparse

from playwright.sync_api import Page

LEARN_HOST = "learn.uwaterloo.ca"
AUTHENTICATED_PATH_PREFIX = "/d2l/"
LOGIN_PATH_PREFIXES = ("/d2l/login",)


def current_url(page: Page) -> str:
    """Get the page's true, live URL.

    `page.url` is NOT a live read -- it's a value Playwright caches from CDP
    navigation events. During live testing, it was observed to go
    completely stale (frozen on the WatIAM/ADFS URL, never updating again
    for the rest of the process) after the WatIAM -> Duo -> LEARN redirect
    chain on a first-time login, even though the browser had genuinely
    finished navigating on screen and cookies were saved correctly.
    Reused/already-authenticated sessions (a single same-origin load, no
    multi-hop federation redirect) never triggered it.

    page.evaluate("window.location.href") performs a real round trip into
    the renderer and reads its actual current location, bypassing whatever
    caused Playwright's own navigation-event bookkeeping to stop updating.
    """
    return page.evaluate("() => window.location.href")


def is_authenticated(page: Page, url: str) -> bool:
    parsed = urlparse(url)

    if parsed.hostname != LEARN_HOST:
        return False
    if not parsed.path.startswith(AUTHENTICATED_PATH_PREFIX):
        return False
    if any(parsed.path.startswith(p) for p in LOGIN_PATH_PREFIXES):
        return False

    if page.locator('input[type="password"]').count() > 0:
        return False

    return True


class BrowserClosedError(Exception):
    """Raised when the user closes the browser/tab before authentication completes."""


def wait_for_authentication(page: Page, timeout_seconds: int = 900, poll_interval: float = 2.0) -> bool:
    """Poll until LEARN authentication is detected or timeout elapses.

    Raises BrowserClosedError if the page or browser is closed by the user.

    Closure is detected via the context's "close" event, not by inferring it
    from exception types on current_url()/page.locator(...). Those calls can
    throw transiently mid-navigation (e.g. "Execution context was destroyed")
    during the normal WatIAM -> Duo -> LEARN redirect chain, which is not a
    closure and must not abort the wait. page.is_closed() also isn't
    reliable here: it only reflects an individual *tab* closing, not the
    whole persistent-context browser *process* being killed (e.g. closing
    the last window) -- that case was observed to hang this loop forever
    before this fix, since the swallowed exception just looked like
    "not authenticated yet, keep waiting."
    """
    browser_closed = False

    def _on_close(*_args) -> None:
        nonlocal browser_closed
        browser_closed = True

    page.context.on("close", _on_close)
    try:
        deadline = time.monotonic() + timeout_seconds
        last_url = None
        while time.monotonic() < deadline:
            if browser_closed or page.is_closed():
                raise BrowserClosedError("The browser was closed before authentication completed.")
            try:
                url = current_url(page)
                authed = is_authenticated(page, url)
            except Exception:
                # Likely a transient mid-navigation error, not closure --
                # closure is caught above via browser_closed/is_closed().
                time.sleep(poll_interval)
                continue

            if url != last_url:
                print(f"  URL changed: {url}")
                last_url = url
            if authed:
                return True
            time.sleep(poll_interval)
        return False
    finally:
        page.context.remove_listener("close", _on_close)
