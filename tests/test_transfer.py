"""Moving a session to a server: export, ssh push, and `auth --stdin`. No network, no real ssh."""

import importlib.util
import io
import subprocess
from pathlib import Path

import pytest

from learn import auth, paste, transfer
from learn.exceptions import LoginError
from tests.test_login_flow import BASE, NOW, _session

_spec = importlib.util.spec_from_file_location("agent_cli", Path(__file__).resolve().parent.parent / "agent.py")
agent_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_cli)


def test_export_contains_only_learn_cookies_and_round_trips_through_the_paste_parser():
    header = transfer.export_header(_session())
    assert header == "d2lSessionVal=s1; d2lSecureSessionVal=s2; d2lXSRF-Token=xs"
    assert "evil" not in header and "duo_trust" not in header
    again = paste.session_from_blob("Cookie: " + header, base_url=BASE, harvested_at=NOW)
    assert {c.name: c.value for c in again.cookies} == {"d2lSessionVal": "s1", "d2lSecureSessionVal": "s2", "d2lXSRF-Token": "xs"}
    assert again.xsrf == "xs"


def test_push_sends_cookies_on_stdin_only_never_in_argv():
    calls = []

    def runner(argv, input=None, check=False):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 0)

    assert transfer.push(_session(), "me@server", runner=runner) == 0
    argv, payload = calls[0]
    assert argv[:3] == ["ssh", "--", "me@server"]
    assert argv[3] == "cd ~/plnr && .venv/bin/python agent.py auth --stdin"
    assert not any("s1" in part or "s2" in part for part in argv)
    assert payload == b"d2lSessionVal=s1; d2lSecureSessionVal=s2; d2lXSRF-Token=xs\n"


def test_push_reports_a_nonzero_ssh_exit():
    assert transfer.push(_session(), "h", runner=lambda argv, input=None, check=False: subprocess.CompletedProcess(argv, 255)) == 255


@pytest.mark.parametrize("host", ["", "-oProxyCommand=evil", "a b", "host\n"])
def test_push_rejects_hosts_that_ssh_could_read_as_options_or_that_contain_whitespace(host):
    with pytest.raises(LoginError):
        transfer.push(_session(), host, runner=lambda *a, **k: pytest.fail("ssh must not run"))


def test_remote_command_quotes_hostile_paths_but_keeps_tilde_expansion():
    cmd = transfer.remote_command("~/my plnr; rm -rf /", "py thon")
    assert cmd == "cd ~/'my plnr; rm -rf /' && 'py thon' agent.py auth --stdin"
    assert transfer.remote_command("/srv/plnr") == "cd /srv/plnr && .venv/bin/python agent.py auth --stdin"


def test_read_blob_refuses_a_terminal_and_oversized_input():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    with pytest.raises(LoginError, match="--paste"):
        transfer.read_blob(Tty("x"))
    with pytest.raises(LoginError, match="too large"):
        transfer.read_blob(io.StringIO("a" * (transfer.MAX_BLOB_BYTES + 1)))
    assert transfer.read_blob(io.StringIO("d2lSessionVal=a")) == "d2lSessionVal=a"


# ---- authenticate_from_blob / CLI ------------------------------------------------------------


def test_blob_is_stored_only_after_live_verification(monkeypatch):
    stored = []
    monkeypatch.setattr(auth, "_store_verified", stored.append)
    blob = "Cookie: d2lSessionVal=a; d2lSecureSessionVal=b"

    monkeypatch.setattr(auth, "verify", lambda value, school=None: None)
    with pytest.raises(LoginError, match="could not be verified"):
        auth.authenticate_from_blob(blob)
    assert stored == []

    monkeypatch.setattr(auth, "verify", lambda value, school=None: "user-1")
    result = auth.authenticate_from_blob(blob)
    assert stored == [result] and result.user_id == "user-1"


def test_cli_stdin_mode(monkeypatch, capsys):
    got = []
    monkeypatch.setattr(agent_cli.auth, "authenticate_from_blob", got.append)
    monkeypatch.setattr("sys.stdin", io.StringIO("d2lSessionVal=a; d2lSecureSessionVal=b"))
    assert agent_cli.run_auth(["--stdin"]) == 0
    assert got == ["d2lSessionVal=a; d2lSecureSessionVal=b"]
    assert "session saved" in capsys.readouterr().out


def test_cli_stdin_mode_reports_a_bad_login(monkeypatch, capsys):
    def reject(blob):
        raise LoginError("login could not be verified")

    monkeypatch.setattr(agent_cli.auth, "authenticate_from_blob", reject)
    monkeypatch.setattr("sys.stdin", io.StringIO("junk"))
    assert agent_cli.run_auth(["--stdin"]) == 1
    assert "could not be verified" in capsys.readouterr().out


def _with_saved_session(monkeypatch, valid=True):
    monkeypatch.setattr(agent_cli.learn_session, "load", lambda: _session())
    monkeypatch.setattr(agent_cli.auth, "verify", lambda value, school=None: "u1" if valid else None)


def test_cli_export_copies_to_clipboard_and_prints_nothing_secret(monkeypatch, capsys):
    _with_saved_session(monkeypatch)
    copied = []
    monkeypatch.setattr(agent_cli.transfer, "copy_to_clipboard", lambda text: copied.append(text) or True)
    assert agent_cli.run_auth(["--export"]) == 0
    assert copied == ["d2lSessionVal=s1; d2lSecureSessionVal=s2; d2lXSRF-Token=xs"]
    assert "s1" not in capsys.readouterr().out


def test_cli_export_without_clipboard_demands_explicit_stdout(monkeypatch, capsys):
    _with_saved_session(monkeypatch)
    monkeypatch.setattr(agent_cli.transfer, "copy_to_clipboard", lambda text: False)
    assert agent_cli.run_auth(["--export"]) == 1
    assert "s1" not in capsys.readouterr().out
    assert agent_cli.run_auth(["--export", "--stdout"]) == 0
    assert "d2lSessionVal=s1" in capsys.readouterr().out


def test_cli_export_and_push_refuse_an_expired_session(monkeypatch, capsys):
    _with_saved_session(monkeypatch, valid=False)
    monkeypatch.setattr(agent_cli.transfer, "push", lambda *a, **k: pytest.fail("must not push an expired session"))
    assert agent_cli.run_auth(["--export"]) == auth.EXIT_SESSION_EXPIRED
    assert agent_cli.run_auth(["--push", "me@server"]) == auth.EXIT_SESSION_EXPIRED


def test_cli_push_passes_flags_through(monkeypatch):
    _with_saved_session(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        agent_cli.transfer, "push", lambda stored, host, directory, python: seen.update(host=host, directory=directory, python=python) or 0
    )
    assert agent_cli.run_auth(["--push", "me@server", "--dir", "/srv/plnr", "--python", "python3"]) == 0
    assert seen == {"host": "me@server", "directory": "/srv/plnr", "python": "python3"}
    assert agent_cli.run_auth(["--push"]) == 1  # missing destination
