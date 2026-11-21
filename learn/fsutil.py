"""Small filesystem helpers shared by the session store and local state files."""

import os
import tempfile
from pathlib import Path


def atomic_write_private(destination: Path, text: str) -> None:
    """Write `text` to `destination` atomically with owner-only (0600) permissions
    inside an owner-only (0700) parent directory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
