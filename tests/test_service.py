import plistlib
from pathlib import Path

from agent import service


def test_plist_runs_the_bot_in_service_mode_and_restarts_on_failure():
    data = plistlib.loads(service.build_plist("/venv/bin/python", Path("/proj"), model="qwen"))
    assert data["Label"] == "com.plnr.agent"
    assert data["ProgramArguments"] == ["/venv/bin/python", "/proj/discord_bot.py", "--service", "--model", "qwen"]
    assert data["WorkingDirectory"] == "/proj"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["ThrottleInterval"] == service.THROTTLE_SECONDS
    assert data["StandardOutPath"].startswith("/proj/.plnr/logs/")


def test_install_writes_the_plist_under_the_given_home(tmp_path):
    path = service.install(home=tmp_path, python="/py")
    assert path == tmp_path / "Library" / "LaunchAgents" / "com.plnr.agent.plist"
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][0] == "/py"


def test_load_and_unload_commands_target_the_gui_domain(tmp_path):
    cmd = service.load_command(tmp_path / "x.plist")
    assert cmd[:2] == ["launchctl", "bootstrap"] and cmd[2].startswith("gui/")
    assert service.unload_command()[1] == "bootout"
