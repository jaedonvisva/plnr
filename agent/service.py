"""macOS launchd LaunchAgent for the always-on Discord bot.

Generates ~/Library/LaunchAgents/com.plnr.agent.plist so the bot starts at login and is
restarted if it exits. It runs `discord_bot.py --service`, which never opens a browser: with
no saved LEARN session it exits 75 (launchd retries every few minutes), and with an expired
one it DMs you to sign in. Nothing here loads the job unless you pass --load.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

LABEL = "com.plnr.agent"
THROTTLE_SECONDS = 300
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / ".plnr" / "logs"


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(python: str, project_root: Path, model: str | None = None) -> bytes:
    arguments = [python, str(project_root / "discord_bot.py"), "--service"]
    if model:
        arguments += ["--model", model]
    log_dir = project_root / ".plnr" / "logs"
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(project_root),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": THROTTLE_SECONDS,
            "StandardOutPath": str(log_dir / "bot.out.log"),
            "StandardErrorPath": str(log_dir / "bot.err.log"),
        }
    )


def install(model: str | None = None, *, home: Path | None = None, python: str | None = None) -> Path:
    if sys.platform != "darwin" and home is None:
        raise RuntimeError("install-service only supports macOS launchd")
    target = plist_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_plist(python or sys.executable, PROJECT_ROOT, model))
    return target


def load_command(path: Path) -> list[str]:
    import os

    return ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]


def unload_command() -> list[str]:
    import os

    return ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"]
