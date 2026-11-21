"""Move a verified LEARN session from a machine with a browser to one without (a server).

    laptop:  agent.py auth                       (real browser sign-in, Duo)
             agent.py auth --push user@server    (ships the session over SSH)
    server:  agent.py auth --stdin               (what --push runs; reads the cookies from
                                                  stdin, verifies them against LEARN, saves)

Only the two LEARN session cookies (plus an XSRF cookie if present) are exported. They are a
working login, so they travel exclusively on the ssh process's stdin -- never in argv (visible
in `ps`), never in a file, never printed unless the user explicitly asks for --stdout.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable

from .exceptions import LoginError
from .session import ALLOWED_COOKIE_NAMES, Session

DEFAULT_REMOTE_DIR = "~/plnr"
DEFAULT_REMOTE_PYTHON = ".venv/bin/python"
MAX_BLOB_BYTES = 64 * 1024


def export_header(value: Session) -> str:
    """`name=value; name=value` for the session's allowed cookies (no "Cookie:" prefix)."""
    pairs = [f"{c.name}={c.value}" for c in value.cookies if c.name.casefold() in ALLOWED_COOKIE_NAMES]
    if not pairs:
        raise LoginError("saved session has no LEARN cookies to export")
    return "; ".join(pairs)


def copy_to_clipboard(text: str) -> bool:
    """macOS clipboard via pbcopy; False when unavailable (use --stdout instead)."""
    if sys.platform != "darwin" or shutil.which("pbcopy") is None:
        return False
    return subprocess.run(["pbcopy"], input=text.encode(), check=False).returncode == 0


def _remote_dir(directory: str) -> str:
    """Quote a remote path for the remote shell while still letting a leading ~/ expand."""
    if directory == "~":
        return "~"
    if directory.startswith("~/"):
        return "~/" + shlex.quote(directory[2:])
    return shlex.quote(directory)


def remote_command(directory: str = DEFAULT_REMOTE_DIR, python: str = DEFAULT_REMOTE_PYTHON) -> str:
    return f"cd {_remote_dir(directory)} && {shlex.quote(python)} agent.py auth --stdin"


def validate_host(host: str) -> str:
    # A leading "-" would be parsed by ssh as an option (e.g. -oProxyCommand=...).
    if not host or host.startswith("-") or any(ch.isspace() for ch in host):
        raise LoginError(f"not a valid ssh destination: {host!r}")
    return host


def push(
    value: Session,
    host: str,
    *,
    directory: str = DEFAULT_REMOTE_DIR,
    python: str = DEFAULT_REMOTE_PYTHON,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    """Run `auth --stdin` on `host` over ssh, feeding it the cookies on stdin. Returns ssh's exit code."""
    destination = validate_host(host)
    payload = export_header(value) + "\n"
    result = runner(
        ["ssh", "--", destination, remote_command(directory, python)],
        input=payload.encode(),
        check=False,
    )
    return result.returncode


def read_blob(stream) -> str:
    """Cookies from a non-terminal stdin (the --stdin mode). A terminal is refused: use --paste."""
    isatty = getattr(stream, "isatty", None)
    if callable(isatty) and isatty():
        raise LoginError("--stdin expects piped input; for typing or pasting use: python agent.py auth --paste")
    data = stream.read(MAX_BLOB_BYTES + 1)
    if len(data) > MAX_BLOB_BYTES:
        raise LoginError("cookie input is too large")
    return data
