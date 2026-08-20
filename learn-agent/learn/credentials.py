"""Opt-in WatIAM username/password auto-fill, isolated from everything else.

This module exists ONLY because the user explicitly requested it, deliberately
accepting the trade-off: a plaintext, fully-reusable WatIAM credential sitting
in a local .env file is a materially higher-stakes secret than the session
cookies this project otherwise relies on. Every other module in this project
still never touches credentials -- this is the one, clearly-labeled exception.

Behavior is opt-in and silent by default: if no .env file / WATIAM_USERNAME /
WATIAM_PASSWORD is present, maybe_autofill_login() does nothing and every
script falls back to the original fully-manual flow.

Duo is NEVER touched here. This only fills the WatIAM username/password form;
whatever happens after submitting (straight through if Duo's browser-trust
cookie is still valid, or a Duo challenge that needs the user) is handled
exactly as before by learn.auth.wait_for_authentication().
"""

import os
from pathlib import Path

from playwright.sync_api import Page

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

USERNAME_FIELD = "#userNameInput"
PASSWORD_FIELD = "#passwordInput"
NEXT_BUTTON = "#nextButton"
"""The username-step's advance control. NOT the same element as
SUBMIT_BUTTON -- confirmed live: on page load, #nextButton is a visible
<span role="button"> while #submitButton is a separate <span
style="display:none">. Clicking #submitButton at this stage does nothing
(it's not interactable); clicking #nextButton is what reveals the
password field and #submitButton."""
SUBMIT_BUTTON = "#submitButton"


def _load_dotenv_if_present() -> None:
    if not ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ENV_FILE)


def load_credentials() -> tuple[str, str] | None:
    """Return (username, password) from .env, or None if not configured."""
    _load_dotenv_if_present()
    username = os.environ.get("WATIAM_USERNAME")
    password = os.environ.get("WATIAM_PASSWORD")
    if username and password:
        return username, password
    return None


def maybe_autofill_login(page: Page, timeout_ms: int = 10000) -> bool:
    """If .env credentials are configured, fill and submit the WatIAM login form.

    Returns True if credentials were filled (Duo, if challenged, still
    requires the user -- untouched by this function). Returns False and does
    nothing if no credentials are configured, or if the page isn't currently
    showing the WatIAM login form (e.g. already authenticated via a
    persisted session).
    """
    creds = load_credentials()
    if creds is None:
        return False

    try:
        page.wait_for_selector(USERNAME_FIELD, timeout=timeout_ms)
    except Exception:
        return False

    username, password = creds
    page.fill(USERNAME_FIELD, username)

    if not page.locator(PASSWORD_FIELD).is_visible():
        # WatIAM's live form is two-step: a username-only screen first, then
        # a password screen revealed only after clicking #nextButton (what a
        # human does by pressing Enter/clicking Next). This is NOT the same
        # control as SUBMIT_BUTTON -- confirmed live, #submitButton stays
        # `display:none` and non-interactable until after #nextButton is
        # clicked, so clicking SUBMIT_BUTTON here silently does nothing.
        page.click(NEXT_BUTTON)
        try:
            page.wait_for_selector(PASSWORD_FIELD, timeout=timeout_ms)
        except Exception:
            return True

    page.fill(PASSWORD_FIELD, password)
    page.click(SUBMIT_BUTTON)
    return True
