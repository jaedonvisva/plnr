"""Same-device LEARN authentication: dedicated-browser CDP sign-in with a hidden-TTY paste fallback.

Ported from agent2learn (https://github.com/ManagementMO/agent2learn, Apache-2.0,
src/agent2learn/auth/__init__.py). The flow:

    ensure_session()  ->  load stored session -> verify via /users/whoami
                           -> (if missing/expired) authenticate() -> store

`authenticate()` has only one fallback, the hidden-TTY paste flow. It never selects
another browser-automation stack and never copies cookies from an everyday browser
profile. This module never reads or fills a credential: the user completes WatIAM
and Duo themselves in the dedicated browser window.
"""

from __future__ import annotations

import re
import shutil
import sys
from urllib.parse import urljoin, urlsplit

import requests

from . import paste, session
from .cdp import UWATERLOO, School
from .exceptions import BrowserNotFound, LoginError

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
USER_AGENT = "plnr/0.1 (+https://github.com/jaedonvisva/plnr)"

EXIT_SESSION_EXPIRED = 75
"""`python agent.py auth --check` exits with this when the saved session is missing or expired."""


def authenticate(school: School = UWATERLOO, *, backend: str = "auto") -> session.Session:
    """Harvest, verify, and persist a minimum same-device LEARN API session."""
    normalized_backend = backend.casefold()
    if normalized_backend == "paste":
        return _authenticate_from_paste(school)
    if normalized_backend not in {"auto", "cdp"}:
        raise LoginError(f"unknown authentication backend: {backend}")

    from . import cdp

    try:
        harvested = cdp.authenticate_browser(school)
    except LoginError as exc:
        safe_message = _safe_cdp_error(exc)
        if normalized_backend == "auto":
            raise LoginError(f"{safe_message}; fallback: python agent.py auth --paste") from None
        raise LoginError(safe_message) from None
    except (OSError, ValueError) as exc:
        message = "dedicated browser authentication could not access local state"
        if normalized_backend == "auto":
            message += "; fallback: python agent.py auth --paste"
        raise LoginError(message) from exc

    stable_id = _verified_id_from_cdp_result(harvested)
    if stable_id is None:
        raise LoginError("login could not be verified; try: python agent.py auth --paste")
    verified = _with_user_id(harvested, stable_id)
    _store_verified(verified)
    return verified


def ensure_session(school: School = UWATERLOO) -> session.Session:
    """Return a verified session, reusing the stored one or signing in if needed."""
    try:
        stored = session.load()
    except ValueError:
        stored = None  # corrupt/unreadable store -> treat as signed out
    if stored is not None and verify(stored, school) is not None:
        return stored
    return authenticate(school)


def verify(value: session.Session, school: School = UWATERLOO) -> str | None:
    """Return the stable D2L identifier after an authenticated, same-origin API check.

    Calls /d2l/api/versions/ then /d2l/api/lp/{ver}/users/whoami. The response is
    reduced immediately to ``Identifier``; display names never enter the returned
    value or the persisted ``Session``. Returns None if the session is expired or
    the check could not complete.
    """
    if _origin(value.base_url) != _origin(school.base_url):
        raise LoginError("session and school must use the same HTTPS origin")

    transport = requests.Session()
    transport.trust_env = False
    transport.cookies.update(value.requests_cookies())
    headers = {"User-Agent": USER_AGENT}
    if value.xsrf:
        headers["X-Csrf-Token"] = value.xsrf

    versions_url = urljoin(value.base_url.rstrip("/") + "/", "d2l/api/versions/")
    try:
        versions_response = transport.get(
            versions_url,
            headers=headers,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            allow_redirects=False,
        )
    except requests.RequestException:
        return None
    try:
        if not 200 <= versions_response.status_code < 300:
            return None
        if "text/html" in versions_response.headers.get("Content-Type", "").casefold():
            return None
        versions = versions_response.json()
    except (ValueError, requests.RequestException):
        return None
    finally:
        versions_response.close()

    for version in _lp_versions(versions):
        whoami_url = urljoin(value.base_url.rstrip("/") + "/", f"d2l/api/lp/{version}/users/whoami")
        try:
            response = transport.get(
                whoami_url,
                headers=headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                allow_redirects=False,
            )
        except requests.RequestException:
            continue
        try:
            if not 200 <= response.status_code < 300:
                continue
            if "text/html" in response.headers.get("Content-Type", "").casefold():
                continue
            payload = response.json()
        except (ValueError, requests.RequestException):
            continue
        finally:
            response.close()

        identifier = _stable_identifier(payload)
        if identifier is not None:
            return identifier
    return None


def clear_profile() -> None:
    """Clear the stored session and remove the dedicated browser profile after consent."""
    from .cdp import profile_path

    try:
        session.clear()
    except OSError as exc:
        raise LoginError(
            "saved API session could not be cleared; close it or fix its permissions and retry"
        ) from exc
    profile = profile_path()
    if profile.is_symlink():
        raise LoginError(f"refusing to remove symlinked profile: {profile}")
    if _profile_is_locked(profile):
        raise LoginError(
            f"dedicated browser profile is in use; close it normally before removing: {profile}"
        )
    if not profile.exists():
        return
    if not profile.is_dir():
        raise LoginError(f"dedicated browser profile is not a directory: {profile}")
    if not _tty(sys.stdin) or not _tty(sys.stdout):
        raise LoginError(f"profile removal requires an interactive confirmation for: {profile}")

    sys.stderr.write(
        "This removes the dedicated browser profile and its Waterloo/Duo remembered state:\n"
        f"  {profile}\n"
        "Type 'yes' to continue: "
    )
    sys.stderr.flush()
    answer = sys.stdin.readline().strip().casefold()
    sys.stderr.write("\n")
    if answer != "yes":
        raise LoginError("profile removal cancelled")
    try:
        shutil.rmtree(profile)
    except OSError as exc:
        raise LoginError("dedicated browser profile could not be removed; close it and retry") from exc


def _authenticate_from_paste(school: School) -> session.Session:
    return authenticate_from_blob(paste.read_hidden_multiline(), school)


def authenticate_from_blob(blob: str, school: School = UWATERLOO) -> session.Session:
    """Parse pasted/piped cookies, verify them against LEARN, and only then store them."""
    pending = paste.session_from_blob(blob, base_url=school.base_url)
    stable_id = verify(pending, school)
    if stable_id is None:
        raise LoginError("login could not be verified; the cookies may be expired or incomplete")
    verified = _with_user_id(pending, stable_id)
    _store_verified(verified)
    return verified


def _store_verified(value: session.Session) -> None:
    try:
        session.store(value)
    except (OSError, ValueError) as exc:
        raise LoginError("verified session could not be saved; check local permissions") from exc


def _with_user_id(value: session.Session, stable_id: str) -> session.Session:
    return session.Session(
        base_url=value.base_url,
        cookies=value.cookies,
        xsrf=value.xsrf,
        harvested_at=value.harvested_at,
        user_id=stable_id,
    )


def _verified_id_from_cdp_result(value: object) -> str | None:
    if isinstance(value, session.Session):
        return value.user_id
    if isinstance(value, str) and value:
        return value
    return None


def _safe_cdp_error(exc: LoginError) -> str:
    """Expose fixed local prerequisite diagnostics, never arbitrary browser exception text."""
    if isinstance(exc, BrowserNotFound):
        return "Chrome or Edge was not found; install one for dedicated-browser sign-in"

    prefix = "authentication stopped at undeclared host "
    raw = str(exc)
    if raw.startswith(prefix):
        host = raw[len(prefix) :].split(";", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host):
            return f"{prefix}{host}"
    return "dedicated browser authentication failed"


def _lp_versions(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, list):
        return ()
    found: list[str] = []
    for product in payload:
        if not isinstance(product, dict) or product.get("ProductCode") != "lp":
            continue
        versions: list[str] = []
        latest = product.get("LatestVersion")
        if isinstance(latest, str):
            versions.append(latest)
        supported = product.get("SupportedVersions")
        if isinstance(supported, list):
            versions.extend(v for v in supported if isinstance(v, str))
        for version in versions:
            if version and version not in found:
                found.append(version)
    return tuple(found)


def _stable_identifier(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("Identifier")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https" or parsed.hostname is None:
        raise LoginError("authentication requires an HTTPS school origin")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise LoginError("school origin has an invalid port") from exc
    return parsed.scheme.casefold(), parsed.hostname.rstrip(".").casefold(), port


def _profile_is_locked(profile) -> bool:
    return any((profile / marker).exists() for marker in ("SingletonLock", "SingletonSocket", "SingletonCookie"))


def _tty(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())
