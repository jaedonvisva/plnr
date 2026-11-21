"""Offline tests for the agent2learn-derived login flow: session store, cookie paste,
CDP origin gating / endpoint validation, the requests transport, and whoami verification.
No browser, no network."""

import json
import os
import stat
from datetime import UTC, datetime

import pytest

from learn import auth, cdp, paste, session
from learn.exceptions import LearnAuthenticationError, LoginError
from learn.transport import SessionTransport

BASE = "https://learn.uwaterloo.ca"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _records(**extra):
    base = [
        {"name": "d2lSessionVal", "value": "s1", "domain": "learn.uwaterloo.ca", "path": "/d2l", "secure": True},
        {"name": "d2lSecureSessionVal", "value": "s2", "domain": "learn.uwaterloo.ca", "path": "/d2l", "secure": True},
        {"name": "d2lXSRF-Token", "value": "xs", "domain": "learn.uwaterloo.ca", "path": "/", "secure": True},
        # must be dropped: other host, and non-whitelisted name
        {"name": "d2lSessionVal", "value": "evil", "domain": "evil.example.com", "path": "/", "secure": True},
        {"name": "duo_trust", "value": "x", "domain": "learn.uwaterloo.ca", "path": "/", "secure": True},
    ]
    return base


def _session():
    return paste.session_from_cookie_records(_records(), base_url=BASE, harvested_at=NOW, user_id="u1")


# ---- session / paste ---------------------------------------------------------------------


def test_harvest_keeps_only_whitelisted_learn_cookies_and_xsrf():
    s = _session()
    assert {c.name for c in s.cookies} == {"d2lSessionVal", "d2lSecureSessionVal", "d2lXSRF-Token"}
    assert all(c.domain == "learn.uwaterloo.ca" for c in s.cookies)
    assert s.xsrf == "xs"
    assert "evil" not in {c.value for c in s.cookies}


def test_missing_required_cookie_is_rejected():
    only_one = [r for r in _records() if r["name"] == "d2lSessionVal"][:1]
    with pytest.raises(paste.PasteError):
        paste.session_from_cookie_records(only_one, base_url=BASE, harvested_at=NOW)


@pytest.mark.parametrize(
    "blob",
    [
        "Cookie: d2lSessionVal=a; d2lSecureSessionVal=b; d2lXSRF-Token=c",
        json.dumps({"cookies": _records()}),
        "Name\tValue\tDomain\tPath\nd2lSessionVal\ta\tlearn.uwaterloo.ca\t/d2l\nd2lSecureSessionVal\tb\tlearn.uwaterloo.ca\t/d2l",
    ],
)
def test_paste_accepts_header_json_and_devtools_table(blob):
    s = paste.session_from_blob(blob, base_url=BASE)
    assert {"d2lSessionVal", "d2lSecureSessionVal"} <= {c.name for c in s.cookies}


def test_paste_refuses_non_tty_input():
    import io

    with pytest.raises(paste.PasteError):
        paste.read_hidden_multiline(input_stream=io.StringIO("x"), output_stream=io.StringIO())


def test_store_load_clear_roundtrip_with_private_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "keyring", None)
    monkeypatch.setattr(session, "AUTH_DIR", tmp_path / ".auth")
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / ".auth" / "session.json")

    assert session.load() is None
    assert session.store(_session()) == "file"
    mode = stat.S_IMODE(os.stat(session.SESSION_FILE).st_mode)
    assert mode == 0o600

    loaded = session.load()
    assert loaded.user_id == "u1" and loaded.xsrf == "xs"
    assert loaded.harvested_at == NOW

    session.clear()
    assert session.load() is None


def test_corrupt_stored_session_raises_value_error(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "keyring", None)
    f = tmp_path / "session.json"
    f.write_text("{not json")
    monkeypatch.setattr(session, "SESSION_FILE", f)
    with pytest.raises(ValueError):
        session.load()


# ---- CDP gate / endpoint validation ------------------------------------------------------


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://learn.uwaterloo.ca/d2l/home", True),
        ("https://adfs.uwaterloo.ca/adfs/ls/", True),
        ("https://api-abc123.duosecurity.com/frame", True),
        ("https://duosecurity.com/x", True),
        ("https://evilduosecurity.com/x", False),  # label boundary, not suffix match
        ("https://duosecurity.com.evil.com/x", False),
        ("http://learn.uwaterloo.ca/d2l", False),  # must be https
        ("https://learn.uwaterloo.ca:8443/d2l", False),  # default port only
        ("https://example.com/", False),
    ],
)
def test_auth_url_allow_list(url, allowed):
    assert cdp._auth_url_allowed(url, cdp.UWATERLOO) is allowed


class _Sender:
    def __init__(self):
        self.sent = []

    def send_without_wait(self, method, params=None):
        self.sent.append((method, params))


def _paused(request_id, url, rtype="Document"):
    return {"method": "Fetch.requestPaused", "params": {"requestId": request_id, "request": {"url": url}, "resourceType": rtype}}


def test_gate_continues_allowed_and_blocks_others_fatal_only_for_documents():
    sender = _Sender()
    gate = cdp._AuthGate(sender, cdp.UWATERLOO)
    gate.handle(_paused("1", "https://learn.uwaterloo.ca/d2l/home"))
    gate.handle(_paused("2", "https://analytics.example.com/t.js", "Script"))
    assert sender.sent[0][0] == "Fetch.continueRequest"
    assert sender.sent[1] == ("Fetch.failRequest", {"requestId": "2", "errorReason": "BlockedByClient"})
    gate.raise_if_blocked()  # a blocked script is not fatal
    with pytest.raises(LoginError, match="analytics.example.com"):
        gate.raise_if_any_blocked()

    gate.handle(_paused("3", "https://phish.example.net/login", "Document"))
    with pytest.raises(LoginError, match="phish.example.net"):
        gate.raise_if_blocked()


def test_gate_decides_each_request_once():
    sender = _Sender()
    gate = cdp._AuthGate(sender, cdp.UWATERLOO)
    gate.handle(_paused("1", "https://learn.uwaterloo.ca/"))
    gate.handle(_paused("1", "https://learn.uwaterloo.ca/"))
    assert len(sender.sent) == 1


def test_browser_metadata_rejects_lookalikes():
    assert cdp._browser_metadata_allowed("Chrome/126.0.6478.62")
    assert cdp._browser_metadata_allowed("Edg/126.0.1")
    assert not cdp._browser_metadata_allowed("NotChrome/1.0")
    assert not cdp._browser_metadata_allowed("Chrome/1.0 (evil)")


def test_active_port_marker_and_loopback(tmp_path):
    marker = tmp_path / "DevToolsActivePort"
    marker.write_text("9222\n/devtools/browser/abc-123\n")
    m = cdp._read_active_port_marker(marker)
    assert (m.port, m.browser_websocket_path) == (9222, "/devtools/browser/abc-123")
    marker.write_text("9222\n/not/devtools\n")
    assert cdp._read_active_port_marker(marker) is None
    assert cdp._loopback_url("ws://127.0.0.1:9222/devtools/browser/x", 9222)
    assert not cdp._loopback_url("ws://evil.com:9222/devtools/browser/x", 9222)
    assert not cdp._loopback_url("ws://127.0.0.1:9999/devtools/browser/x", 9222)


def test_profile_symlink_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(LoginError):
        cdp._validate_profile_path(link)


# ---- transport ----------------------------------------------------------------------------


class _Raw:
    def __init__(self, status, body=b"[]", ctype="application/json", headers=None):
        self.status_code = status
        self.content = body
        self.headers = {"Content-Type": ctype, **(headers or {})}
        self.closed = False

    def close(self):
        self.closed = True


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []
        self.cookies = type("J", (), {"update": lambda self, x: None})()
        self.headers = {}
        self.trust_env = True

    def get(self, url, **kw):
        assert kw["allow_redirects"] is False
        self.urls.append(url)
        return self.responses.pop(0)


def _transport(responses, sleeps=None, **kw):
    t = SessionTransport(_session(), sleep=(sleeps.append if sleeps is not None else (lambda s: None)), **kw)
    t._http = _Http(responses)
    return t


def test_transport_returns_json_response():
    r = _transport([_Raw(200, b'[{"a":1}]')]).get(f"{BASE}/d2l/api/versions/")
    assert r.ok and r.status == 200 and r.json() == [{"a": 1}]


def test_transport_403_json_is_passed_through_not_treated_as_expiry():
    r = _transport([_Raw(403, b'{"Errors":[{"Message":"Not Authorized"}]}')]).get(f"{BASE}/x")
    assert r.status == 403


@pytest.mark.parametrize("raw", [_Raw(200, b"<html><title>Sign in</title>", "text/html"), _Raw(302, b"", "text/plain")])
def test_transport_html_or_redirect_means_expired_session(raw):
    with pytest.raises(LearnAuthenticationError):
        _transport([raw]).get(f"{BASE}/d2l/api/lp/1.62/enrollments/myenrollments/")


def test_transport_retries_429_then_succeeds_honouring_retry_after():
    sleeps = []
    t = _transport([_Raw(429, headers={"Retry-After": "3"}), _Raw(503), _Raw(200, b"[]")], sleeps)
    assert t.get(f"{BASE}/x").ok
    assert sleeps[0] == 3.0 and sleeps[1] == 2.0  # Retry-After, then doubled backoff
    assert len(t._http.urls) == 3


def test_transport_gives_up_after_max_attempts_and_returns_last_status():
    t = _transport([_Raw(503)] * 5)
    assert t.get(f"{BASE}/x").status == 503


def test_transport_never_sends_cookies_to_another_origin():
    with pytest.raises(ValueError):
        _transport([_Raw(200)]).get("https://evil.example.com/d2l/api/versions/")


# ---- verify (whoami) ----------------------------------------------------------------------


class _VResp:
    def __init__(self, status, payload, ctype="application/json"):
        self.status_code = status
        self._payload = payload
        self.headers = {"Content-Type": ctype}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self):
        pass


def _patch_verify_http(monkeypatch, routes):
    calls = []

    class FakeSession:
        trust_env = True
        cookies = type("J", (), {"update": lambda self, x: None})()

        def get(self, url, headers=None, **kw):
            calls.append((url, headers))
            for frag, resp in routes.items():
                if frag in url:
                    return resp
            raise AssertionError(url)

    monkeypatch.setattr(auth.requests, "Session", FakeSession)
    return calls


VERSIONS = [{"ProductCode": "lp", "LatestVersion": "1.62", "SupportedVersions": ["1.47", "1.62"]}]


def test_verify_returns_identifier_and_sends_csrf_header(monkeypatch):
    calls = _patch_verify_http(
        monkeypatch,
        {"/versions/": _VResp(200, VERSIONS), "/users/whoami": _VResp(200, {"Identifier": 12345, "FirstName": "X"})},
    )
    assert auth.verify(_session()) == "12345"
    assert all(h["X-Csrf-Token"] == "xs" for _, h in calls)


def test_verify_treats_html_login_page_as_expired(monkeypatch):
    _patch_verify_http(monkeypatch, {"/versions/": _VResp(200, VERSIONS, "text/html; charset=utf-8")})
    assert auth.verify(_session()) is None


def test_verify_tries_older_lp_versions_when_latest_whoami_fails(monkeypatch):
    routes = {"/versions/": _VResp(200, VERSIONS), "lp/1.62/users/whoami": _VResp(404, {}), "lp/1.47/users/whoami": _VResp(200, {"Identifier": "abc"})}
    _patch_verify_http(monkeypatch, routes)
    assert auth.verify(_session()) == "abc"


def test_verify_rejects_cross_origin_session():
    other = paste.session_from_cookie_records(
        [{**r, "domain": "learn.other.edu"} for r in _records()[:2]], base_url="https://learn.other.edu", harvested_at=NOW
    )
    with pytest.raises(LoginError):
        auth.verify(other)


def test_cdp_error_message_exposes_only_host_not_browser_text():
    assert auth._safe_cdp_error(LoginError("authentication stopped at undeclared host evil.com; fallback: x")) == (
        "authentication stopped at undeclared host evil.com"
    )
    assert auth._safe_cdp_error(LoginError("Runtime.evaluate secret detail")) == "dedicated browser authentication failed"


def test_locked_profile_without_endpoint_is_refused(tmp_path):
    (tmp_path / "SingletonLock").write_text("")
    with pytest.raises(LoginError, match="locked"):
        cdp._acquire_endpoint(tmp_path)


# ---- re-auth on expiry ----------------------------------------------------------------------

EXPIRED = lambda: _Raw(200, b"<html><title>Sign in</title>", "text/html")  # noqa: E731


def _fresh_http(monkeypatch, responses):
    """After re-auth the transport builds a new requests.Session; hand it a scripted one."""
    http = _Http(responses)
    monkeypatch.setattr("learn.transport.requests.Session", lambda: http)
    return http


def test_expired_session_reauths_once_and_retries_the_request(monkeypatch):
    calls = []

    def reauth():
        calls.append(1)
        return _session()

    t = _transport([EXPIRED()], reauth=reauth)
    new_http = _fresh_http(monkeypatch, [_Raw(200, b'[{"ok":true}]')])
    r = t.get(f"{BASE}/d2l/api/le/1.96/1/news/")
    assert r.ok and r.json() == [{"ok": True}]
    assert calls == [1]
    assert t._http is new_http


def test_401_status_also_triggers_reauth(monkeypatch):
    t = _transport([_Raw(401, b"{}")], reauth=_session)
    _fresh_http(monkeypatch, [_Raw(200, b"[]")])
    assert t.get(f"{BASE}/x").ok


def test_reauth_does_not_loop_when_new_session_is_also_rejected(monkeypatch):
    calls = []
    t = _transport([EXPIRED()], reauth=lambda: calls.append(1) or _session())
    _fresh_http(monkeypatch, [EXPIRED()])
    with pytest.raises(LearnAuthenticationError):
        t.get(f"{BASE}/x")
    assert calls == [1]


def test_failed_reauth_raises_auth_error_and_cools_down():
    clock = [1000.0]
    attempts = []

    def reauth():
        attempts.append(1)
        raise LoginError("browser closed")

    t = _transport([EXPIRED(), EXPIRED(), EXPIRED()], reauth=reauth, clock=lambda: clock[0])
    with pytest.raises(LearnAuthenticationError, match="re-authentication failed"):
        t.get(f"{BASE}/x")
    with pytest.raises(LearnAuthenticationError):  # inside cooldown: no second browser
        t.get(f"{BASE}/x")
    assert len(attempts) == 1

    clock[0] += 121  # cooldown over -> may try again
    with pytest.raises(LearnAuthenticationError):
        t.get(f"{BASE}/x")
    assert len(attempts) == 2


def test_no_reauth_callback_means_plain_expiry_error():
    with pytest.raises(LearnAuthenticationError, match="python agent.py auth"):
        _transport([EXPIRED()]).get(f"{BASE}/x")
