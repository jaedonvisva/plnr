import os

from agent import doctor
from agent.doctor import FAIL, OK, WARN, Deps


def by_name(checks):
    return {c.name: c for c in checks}


def healthy(**kw):
    base = dict(
        python_version=(3, 14),
        env={"GEMINI_API_KEY": "k"},
        locate_browser=lambda: "/Applications/Google Chrome",
        load_session=lambda: object(),
        verify_session=lambda s: "user-1",
    )
    base.update(kw)
    return Deps(**base)


def test_healthy_setup_reports_everything_good():
    checks = doctor.run_checks(healthy())
    assert all(c.status == OK for c in checks)
    assert doctor.next_step(checks) is None and doctor.exit_code(checks) == 0
    assert "Everything looks good." in doctor.render(checks)


def test_missing_session_is_the_next_step():
    checks = doctor.run_checks(healthy(load_session=lambda: None))
    c = by_name(checks)
    assert c["Saved session"].status == FAIL
    assert "LEARN session" not in c  # nothing to verify
    assert doctor.next_step(checks) == "Run: python agent.py auth" and doctor.exit_code(checks) == 1


def test_expired_session_detected_via_verify():
    c = by_name(doctor.run_checks(healthy(verify_session=lambda s: None)))
    assert c["LEARN session"].status == FAIL and "expired" in c["LEARN session"].detail


def test_network_error_during_verify_is_a_warning_not_a_failure():
    def boom(_):
        raise OSError("no route")

    checks = doctor.run_checks(healthy(verify_session=boom))
    assert by_name(checks)["LEARN session"].status == WARN and doctor.exit_code(checks) == 0


def test_corrupt_session_store():
    def corrupt():
        raise ValueError("bad")

    c = by_name(doctor.run_checks(healthy(load_session=corrupt)))
    assert c["Saved session"].status == FAIL and "unreadable" in c["Saved session"].detail


def test_failures_are_prioritised_over_warnings():
    checks = doctor.run_checks(healthy(env={}, load_session=lambda: None))
    assert by_name(checks)["Gemini API key"].status == WARN
    assert doctor.next_step(checks) == "Run: python agent.py auth"  # fail before the earlier-ordered warn


def test_no_browser_is_a_warning_pointing_at_paste():
    def none():
        raise FileNotFoundError

    checks = doctor.run_checks(healthy(locate_browser=none))
    assert by_name(checks)["Browser for sign-in"].status == WARN
    assert "auth --paste" in by_name(checks)["Browser for sign-in"].fix


def test_old_python_fails():
    assert by_name(doctor.run_checks(healthy(python_version=(3, 9))))["Python"].status == FAIL


def test_group_readable_session_file_fails(tmp_path):
    f = tmp_path / "session.json"
    f.write_text("{}")
    os.chmod(f, 0o644)
    c = by_name(doctor.run_checks(healthy(session_file=f)))
    assert c["Session file permissions"].status == FAIL and c["Session file permissions"].fix == f"Run: chmod 600 {f}"
    os.chmod(f, 0o600)
    assert by_name(doctor.run_checks(healthy(session_file=f)))["Session file permissions"].status == OK


def test_secrets_are_never_rendered():
    checks = doctor.run_checks(healthy(env={"GEMINI_API_KEY": "sk-SECRET", "DISCORD_BOT_TOKEN": "tok-SECRET", "DISCORD_ALLOWED_USER_ID": "1"}))
    text = doctor.render(checks)
    assert "SECRET" not in text and "Discord bot (optional): token and allowed user id set" in text


def test_half_configured_discord_warns():
    c = by_name(doctor.run_checks(healthy(env={"GEMINI_API_KEY": "k", "DISCORD_BOT_TOKEN": "t"})))
    assert c["Discord bot (optional)"].status == WARN
