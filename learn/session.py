"""Minimal local LEARN API session: a few whitelisted cookies plus the CSRF token.

Ported from agent2learn (https://github.com/ManagementMO/agent2learn, Apache-2.0,
src/agent2learn/session.py). After sign-in we do NOT keep the browser around:
only the cookies the Valence API needs are exported, scoped to the LEARN host,
and stored in the OS keyring when available (falling back to a 0600 file under
.auth/). Identity-provider (WatIAM/Duo) cookies and display names never enter
this projection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.cookies import RequestsCookieJar

from .fsutil import atomic_write_private

try:
    import keyring as _keyring
except Exception:  # keyring is optional
    keyring: Any = None
else:
    keyring = _keyring

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = PROJECT_ROOT / ".auth"
SESSION_FILE = AUTH_DIR / "session.json"

_KEYRING_SERVICE = "plnr-learn"
_KEYRING_USERNAME = "session"
_KEYRING_TIMEOUT_SECONDS = 3.0
_KEYRING_HELPER = """
import json
import sys

request = json.load(sys.stdin)
result = {"ok": False}
try:
    import keyring

    operation = request.get("operation")
    service = request.get("service")
    username = request.get("username")
    if operation == "get":
        value = keyring.get_password(service, username)
        if value is not None and not isinstance(value, str):
            raise TypeError
        result = {"ok": True, "value": value}
    elif operation == "set":
        password = request.get("password")
        if not isinstance(password, str):
            raise TypeError
        keyring.set_password(service, username, password)
        result = {"ok": True}
    elif operation == "delete":
        keyring.delete_password(service, username)
        result = {"ok": True}
except Exception:
    result = {"ok": False}
sys.stdout.write(json.dumps(result, separators=(",", ":")))
"""
_SCHEMA_KEYS = frozenset({"base_url", "cookies", "harvested_at", "user_id", "xsrf"})
_COOKIE_KEYS = frozenset({"domain", "name", "path", "secure", "value"})
ALLOWED_COOKIE_NAMES = frozenset(
    {"d2lsessionval", "d2lsecuresessionval", "xsrf-token", "xsrf_token", "d2lxsrf-token"}
)


@dataclass(frozen=True)
class SessionCookie:
    """One browser-exported cookie with its original scope retained."""

    name: str
    value: str
    domain: str
    path: str
    secure: bool


@dataclass
class Session:
    """The minimum local API session projection.

    Cookies are filtered to the configured LEARN host at construction and again
    when a request jar is made, so later mutation of this non-frozen dataclass
    cannot widen the request scope accidentally.
    """

    base_url: str
    cookies: tuple[SessionCookie, ...]
    xsrf: str | None
    harvested_at: datetime
    user_id: str | None

    def __post_init__(self) -> None:
        self.base_url = _normalize_base_url(self.base_url)
        self.cookies = tuple(self.cookies)
        if self.xsrf is not None and not isinstance(self.xsrf, str):
            raise ValueError("session xsrf must be a string or null")
        if self.user_id is not None and not isinstance(self.user_id, str):
            raise ValueError("session user_id must be a string or null")
        if not isinstance(self.harvested_at, datetime):
            raise ValueError("session harvested_at must be a datetime")
        if self.harvested_at.tzinfo is None or self.harvested_at.utcoffset() is None:
            raise ValueError("session harvested_at must be timezone-aware")
        self.harvested_at = self.harvested_at.astimezone(UTC)

        host = _base_host(self.base_url)
        scoped: list[SessionCookie] = []
        for cookie in self.cookies:
            _validate_cookie(cookie)
            if cookie.name.casefold() in ALLOWED_COOKIE_NAMES and _cookie_belongs_to_host(
                cookie.domain, host
            ):
                scoped.append(cookie)
        self.cookies = tuple(scoped)

    def age(self) -> timedelta:
        return datetime.now(UTC) - self.harvested_at

    def requests_cookies(self) -> RequestsCookieJar:
        """A requests jar containing only cookies scoped to this session's host."""
        jar = requests.cookies.RequestsCookieJar()
        host = _base_host(self.base_url)
        for cookie in self.cookies:
            _validate_cookie(cookie)
            if cookie.name.casefold() not in ALLOWED_COOKIE_NAMES or not _cookie_belongs_to_host(
                cookie.domain, host
            ):
                continue
            jar.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path,
                secure=cookie.secure,
            )
        return jar


def store(value: Session) -> str:
    """Persist a validated session and return the backend that accepted it."""
    if not isinstance(value, Session):
        raise ValueError("session value has an invalid schema")
    validated = Session(
        base_url=value.base_url,
        cookies=value.cookies,
        xsrf=value.xsrf,
        harvested_at=value.harvested_at,
        user_id=value.user_id,
    )
    blob = _encode(validated)

    if keyring is not None:
        succeeded, _ = _keyring_call("set", blob)
        if succeeded:
            _remove_file_quietly(SESSION_FILE)
            return "keyring"
        # Keyring backends commonly fail (no D-Bus, locked keychain). That is an expected
        # storage choice, not an error to surface. Drop any stale keyring value so a later
        # process can't prefer it over the file fallback.
        _keyring_call("delete")

    atomic_write_private(SESSION_FILE, blob)
    return "file"


def load() -> Session | None:
    """Load the keyring session, then the protected local-file fallback."""
    if keyring is not None:
        succeeded, blob = _keyring_call("get")
        if succeeded and blob is not None:
            return _decode(blob)

    blob = _read_file()
    if blob is None:
        return None
    return _decode(blob)


def clear() -> None:
    """Remove the exported session from both storage backends."""
    if keyring is not None:
        _keyring_call("delete")
    # An explicit clear must not claim success while a file-backed session remains on disk.
    try:
        os.unlink(SESSION_FILE)
    except FileNotFoundError:
        return


def _keyring_call(operation: str, password: str | None = None) -> tuple[bool, object]:
    """Run one keyring operation without letting a native backend hang the CLI.

    macOS Keychain calls can wait indefinitely for an OS-level access decision, so
    on macOS the native backend runs in a short-lived child process with a timeout.
    """
    if keyring is None:
        return False, None
    if operation not in {"get", "set", "delete"}:
        raise ValueError("unsupported keyring operation")
    if _keyring_requires_isolation():
        return _keyring_subprocess(operation, password)
    try:
        if operation == "get":
            return True, keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        if operation == "set":
            if password is None:
                raise ValueError("keyring password is required")
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, password)
        else:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:
        return False, None
    return True, None


def _keyring_requires_isolation() -> bool:
    if sys.platform != "darwin" or keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    return type(backend).__module__ == "keyring.backends.macOS"


def _keyring_subprocess(operation: str, password: str | None) -> tuple[bool, object]:
    request = json.dumps(
        {
            "operation": operation,
            "password": password,
            "service": _KEYRING_SERVICE,
            "username": _KEYRING_USERNAME,
        },
        separators=(",", ":"),
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _KEYRING_HELPER],
            input=request,
            capture_output=True,
            check=False,
            text=True,
            timeout=_KEYRING_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if completed.returncode != 0:
        return False, None
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError):
        return False, None
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False, None
    if operation == "get":
        value = result.get("value")
        return (True, value) if value is None or isinstance(value, str) else (False, None)
    return True, None


def _encode(value: Session) -> str:
    payload: dict[str, object] = {
        "base_url": value.base_url,
        "cookies": [
            {
                "domain": c.domain,
                "name": c.name,
                "path": c.path,
                "secure": c.secure,
                "value": c.value,
            }
            for c in value.cookies
        ],
        "harvested_at": value.harvested_at.isoformat().replace("+00:00", "Z"),
        "user_id": value.user_id,
        "xsrf": value.xsrf,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _decode(blob: object) -> Session:
    if not isinstance(blob, str):
        raise ValueError("stored session schema is not text")
    try:
        raw: Any = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError("stored session is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != _SCHEMA_KEYS:
        raise ValueError("stored session schema is invalid")

    base_url = raw["base_url"]
    cookies_raw = raw["cookies"]
    harvested_at = raw["harvested_at"]
    user_id = raw["user_id"]
    xsrf = raw["xsrf"]
    if not isinstance(base_url, str) or not isinstance(cookies_raw, list):
        raise ValueError("stored session schema is invalid")
    if not isinstance(harvested_at, str):
        raise ValueError("stored session schema is invalid")
    if user_id is not None and not isinstance(user_id, str):
        raise ValueError("stored session schema is invalid")
    if xsrf is not None and not isinstance(xsrf, str):
        raise ValueError("stored session schema is invalid")

    cookies: list[SessionCookie] = []
    for raw_cookie in cookies_raw:
        if not isinstance(raw_cookie, dict) or set(raw_cookie) != _COOKIE_KEYS:
            raise ValueError("stored session cookie schema is invalid")
        cookies.append(
            SessionCookie(
                name=raw_cookie["name"],
                value=raw_cookie["value"],
                domain=raw_cookie["domain"],
                path=raw_cookie["path"],
                secure=raw_cookie["secure"],
            )
        )

    return Session(
        base_url=base_url,
        cookies=tuple(cookies),
        xsrf=xsrf,
        harvested_at=_parse_harvested_at(harvested_at),
        user_id=user_id,
    )


def _parse_harvested_at(value: str) -> datetime:
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("stored session harvested_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored session harvested_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _normalize_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("session base_url must be a string")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("session base_url has an invalid host") from exc
    if parsed.scheme.casefold() != "https":
        raise ValueError("session base_url must use HTTPS")
    if parsed.hostname is None or parsed.username is not None or parsed.password is not None:
        raise ValueError("session base_url must contain a host without credentials")
    if parsed.query or parsed.fragment or port is None and ":" in parsed.netloc:
        raise ValueError("session base_url must be a plain configured host")
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _base_host(base_url: str) -> str:
    hostname = urlsplit(base_url).hostname
    if hostname is None:
        raise ValueError("session base_url must contain a host")
    return hostname.rstrip(".").casefold()


def _validate_cookie(cookie: SessionCookie) -> None:
    if not isinstance(cookie, SessionCookie):
        raise ValueError("session cookie schema is invalid")
    if not isinstance(cookie.name, str) or not cookie.name:
        raise ValueError("session cookie schema is invalid")
    if not isinstance(cookie.value, str):
        raise ValueError("session cookie schema is invalid")
    if not isinstance(cookie.domain, str) or not cookie.domain.strip():
        raise ValueError("session cookie schema is invalid")
    if not isinstance(cookie.path, str) or not cookie.path.startswith("/"):
        raise ValueError("session cookie schema is invalid")
    if not isinstance(cookie.secure, bool):
        raise ValueError("session cookie schema is invalid")


def _cookie_belongs_to_host(domain: str, host: str) -> bool:
    return domain.lstrip(".").rstrip(".").casefold() == host


def _read_file() -> str | None:
    if SESSION_FILE.is_symlink():
        raise ValueError("stored session must not be a symlink")
    try:
        with open(SESSION_FILE, encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise ValueError("stored session is not valid JSON") from exc


def _remove_file_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        return
