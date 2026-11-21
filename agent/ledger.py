"""Durable state for the proactive agent: what was already sent, what the user marked done,
how long to stay quiet, and which items each sent message listed (so "done 2" has a referent).

Lives in `.plnr/ledger.json` (gitignored, owner-only). It holds item ids, course labels and
assignment names the user already sees in LEARN -- never announcement text or grades.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from learn.fsutil import atomic_write_private

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER_PATH = PROJECT_ROOT / ".plnr" / "ledger.json"
SCHEMA_VERSION = 1
SENT_RETENTION = timedelta(days=21)
MAX_REMEMBERED_MESSAGES = 40


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Ledger:
    """Small JSON-backed store. Every mutation saves immediately; a corrupt or unknown-version
    file is treated as empty rather than crashing the daemon."""

    def __init__(self, path: Path = DEFAULT_LEDGER_PATH):
        self.path = path
        self._data = self._load()

    # ---- persistence ------------------------------------------------------------------

    def _empty(self) -> dict[str, Any]:
        return {"version": SCHEMA_VERSION, "sent": {}, "done": {}, "muted_until": None, "messages": [], "signin_notified": False}

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            return self._empty()
        merged = self._empty()
        merged.update({k: v for k, v in data.items() if k in merged})
        return merged

    def _save(self) -> None:
        atomic_write_private(self.path, json.dumps(self._data, sort_keys=True, ensure_ascii=False) + "\n")

    # ---- sent keys --------------------------------------------------------------------

    def was_sent(self, key: str) -> bool:
        return key in self._data["sent"]

    def mark_sent(self, keys: list[str], now: datetime) -> None:
        for key in keys:
            self._data["sent"][key] = now.isoformat()
        cutoff = now - SENT_RETENTION
        self._data["sent"] = {
            k: v for k, v in self._data["sent"].items() if (_parse(v) or now) >= cutoff
        }
        self._save()

    # ---- done items -------------------------------------------------------------------

    def is_done(self, uid: str) -> bool:
        return uid in self._data["done"]

    def mark_done(self, uid: str, label: str, now: datetime) -> None:
        self._data["done"][uid] = {"label": label, "at": now.isoformat()}
        self._save()

    def undo_last_done(self) -> str | None:
        """Un-mark the most recently marked item. Returns its label, or None if nothing was done."""
        done = self._data["done"]
        if not done:
            return None
        uid = max(done, key=lambda u: done[u]["at"])
        label = done.pop(uid)["label"]
        self._save()
        return label

    # ---- mute -------------------------------------------------------------------------

    def muted_until(self) -> datetime | None:
        return _parse(self._data["muted_until"])

    def is_muted(self, now: datetime) -> bool:
        until = self.muted_until()
        return until is not None and now < until

    def mute_until(self, until: datetime | None) -> None:
        self._data["muted_until"] = until.isoformat() if until else None
        self._save()

    # ---- what each sent message listed --------------------------------------------------

    def remember_message(self, message_id: int, items: list[dict[str, str]], now: datetime) -> None:
        """`items` are {"uid", "label"} in the order they were numbered in the message."""
        if not items:
            return
        messages = self._data["messages"]
        messages.append({"id": str(message_id), "at": now.isoformat(), "items": items})
        del messages[:-MAX_REMEMBERED_MESSAGES]
        self._save()

    def items_for(self, message_id: int | None) -> list[dict[str, str]]:
        """Items of the given message, or of the most recent message that listed any."""
        messages = self._data["messages"]
        if message_id is not None:
            for entry in reversed(messages):
                if entry["id"] == str(message_id):
                    return list(entry["items"])
            return []
        return list(messages[-1]["items"]) if messages else []

    # ---- sign-in notice ---------------------------------------------------------------

    @property
    def signin_notified(self) -> bool:
        return bool(self._data["signin_notified"])

    def set_signin_notified(self, value: bool) -> None:
        if self._data["signin_notified"] != value:
            self._data["signin_notified"] = value
            self._save()
