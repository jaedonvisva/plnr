"""`python agent.py doctor`: diagnose setup problems and name ONE next step.

Every check is a pure function of injected dependencies so the whole report is
testable offline. Nothing here prints a secret: it reports whether a key is *set*,
never its value. Idea from agent2learn's `doctor`
(https://github.com/ManagementMO/agent2learn, Apache-2.0).
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str | None = None


@dataclass
class Deps:
    """Everything doctor touches in the outside world."""

    python_version: tuple[int, int] = (sys.version_info.major, sys.version_info.minor)
    env: Mapping[str, str] | None = None
    locate_browser: Callable[[], Path] | None = None
    load_session: Callable[[], object | None] | None = None
    verify_session: Callable[[object], str | None] | None = None
    session_file: Path | None = None
    triage_available: Callable[[], bool] | None = None


def run_checks(deps: Deps) -> list[Check]:
    env = deps.env if deps.env is not None else os.environ
    checks: list[Check] = []

    # Python
    if deps.python_version >= (3, 11):
        checks.append(Check("Python", OK, f"{deps.python_version[0]}.{deps.python_version[1]}"))
    else:
        checks.append(Check("Python", FAIL, f"{deps.python_version[0]}.{deps.python_version[1]} is too old", "Install Python 3.11 or newer."))

    # Browser (needed only to sign in)
    if deps.locate_browser is not None:
        try:
            checks.append(Check("Browser for sign-in", OK, str(deps.locate_browser())))
        except Exception:
            checks.append(
                Check("Browser for sign-in", WARN, "Chrome/Edge not found", "Install Chrome or Edge, or sign in with: python agent.py auth --paste")
            )

    # Session
    session = None
    if deps.load_session is not None:
        try:
            session = deps.load_session()
        except ValueError:
            checks.append(Check("Saved session", FAIL, "stored session is unreadable", "Run: python agent.py auth"))
            session = False  # distinguishes "corrupt" from "missing"
        else:
            if session is None:
                checks.append(Check("Saved session", FAIL, "none saved", "Run: python agent.py auth"))
    if session and deps.verify_session is not None:
        try:
            identifier = deps.verify_session(session)
        except Exception:
            identifier = None
            checks.append(Check("LEARN session", WARN, "could not reach LEARN", "Check your network connection, then re-run doctor."))
        else:
            if identifier:
                checks.append(Check("LEARN session", OK, "saved session is accepted by LEARN"))
            else:
                checks.append(Check("LEARN session", FAIL, "expired or rejected", "Run: python agent.py auth"))
    if deps.session_file is not None and deps.session_file.exists():
        mode = stat.S_IMODE(os.stat(deps.session_file).st_mode)
        if mode & 0o077:
            checks.append(Check("Session file permissions", FAIL, f"mode {oct(mode)} is readable by others", f"Run: chmod 600 {deps.session_file}"))
        else:
            checks.append(Check("Session file permissions", OK, "owner-only"))

    # LLM backends (values are never shown)
    if env.get("GEMINI_API_KEY"):
        checks.append(Check("Gemini API key", OK, "set"))
    else:
        checks.append(
            Check("Gemini API key", WARN, "GEMINI_API_KEY not set (the default backend)", "Add GEMINI_API_KEY to .env, or run with --model qwen.")
        )

    # Optional pieces
    if deps.triage_available is not None:
        checks.append(Check("Announcement triage (optional)", OK if deps.triage_available() else WARN, "installed" if deps.triage_available() else "not installed", None if deps.triage_available() else "Optional: pip install -r requirements-triage.txt"))
    token, user = env.get("DISCORD_BOT_TOKEN"), env.get("DISCORD_ALLOWED_USER_ID")
    if token and user:
        checks.append(Check("Discord bot (optional)", OK, "token and allowed user id set"))
    elif token or user:
        checks.append(Check("Discord bot (optional)", WARN, "only one of DISCORD_BOT_TOKEN / DISCORD_ALLOWED_USER_ID is set", "Set both, or remove the other."))
    return checks


def next_step(checks: list[Check]) -> str | None:
    """The single most important fix: failures first, then warnings, in check order."""
    for level in (FAIL, WARN):
        for c in checks:
            if c.status == level and c.fix:
                return c.fix
    return None


def render(checks: list[Check]) -> str:
    lines = [f"  {_ICON[c.status]} {c.name}: {c.detail}" for c in checks]
    step = next_step(checks)
    lines.append("")
    lines.append(f"Next step: {step}" if step else "Everything looks good.")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
