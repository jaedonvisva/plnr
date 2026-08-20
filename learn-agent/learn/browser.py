"""Browser lifecycle: launches a persistent, visible Chromium context.

Persistence uses Playwright's `launch_persistent_context`, which stores the
full browser profile (cookies, local storage, session storage) on disk under
AUTH_DIR. This directory is a bearer credential for your LEARN session --
anyone with access to it can act as you until Waterloo's SSO session expires.
It is gitignored and must never be committed, synced, or printed.
"""

import shutil
from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = PROJECT_ROOT / ".auth" / "browser-profile"

LEARN_URL = "https://learn.uwaterloo.ca"


def launch_browser() -> tuple[BrowserContext, object]:
    """Launch a visible, persistent Chromium context.

    Returns (context, playwright_instance). Caller is responsible for
    calling playwright_instance.stop() when done (context.close() alone
    does not stop the driver).
    """
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        str(AUTH_DIR),
        headless=False,
        viewport={"width": 1280, "height": 900},
    )
    return context, playwright


def clear_session() -> bool:
    """Delete the local persisted session. Returns True if anything was removed."""
    auth_root = AUTH_DIR.parent
    if auth_root.exists():
        shutil.rmtree(auth_root)
        return True
    return False
