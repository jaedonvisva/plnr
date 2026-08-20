"""Reusable, code-enforced limits on tool-call behavior within one agent
turn. Defense-in-depth against a runaway or adversarially-triggered tool
loop (e.g. prompt-injected LEARN content trying to make the model call
search tools repeatedly) -- independent of and in addition to
MAX_TOOL_ROUNDS in agent/loop.py.

This module intentionally does NOT implement cross-request/cross-session
rate limiting (requests-per-minute, per-sender quotas). That needs identity
(who is asking) and persistent state across invocations, which only exists
once there's a real caller with a notion of "sender" -- the future
iMessage daemon. TurnGuard is written so that future layer can reuse it
per-sender (`TurnGuard()` per conversation) rather than needing new
plumbing; building the cross-session part now would be speculative.
"""

import json
from dataclasses import dataclass, field
from typing import Any

MAX_TOOL_CALLS_PER_ROUND = 10
"""A single model response can request multiple tool calls at once
(agent/loop.py's `for call in response.message.tool_calls`). Cap how many
of those actually execute, independent of MAX_TOOL_ROUNDS."""

MAX_TOOL_CALLS_PER_TURN = 30
"""Total individual tool invocations across every round of one turn.
MAX_TOOL_ROUNDS(20) x MAX_TOOL_CALLS_PER_ROUND(10) allows a 200-call worst
case; this caps the realistic total lower while still comfortably covering
observed legitimate usage (the heaviest real cross-course query seen live
used ~11 calls)."""

MAX_TOOL_RESULT_BYTES_PER_TURN = 500_000
"""Cumulative size (json-serialized) of tool results returned within one
turn. Bounds worst-case context growth/cost even from many small-but-legal
results, on top of agent/serialize.py's per-field truncation."""


@dataclass
class TurnGuard:
    """Tracks tool-call count, duplicate calls, and cumulative result size
    for one agent turn. Call before_call() before executing a tool; it
    returns an error dict (fail closed) if the call should be refused, or
    None if it's fine to proceed. Call record_result() after executing to
    update the size budget."""

    calls_this_turn: int = 0
    _seen_calls: set[tuple] = field(default_factory=set)
    _result_bytes: int = 0

    def before_call(self, name: str, arguments: dict) -> dict[str, Any] | None:
        if self.calls_this_turn >= MAX_TOOL_CALLS_PER_TURN:
            return {
                "error": True,
                "type": "tool_call_limit_exceeded",
                "message": f"This turn has already made {self.calls_this_turn} tool calls "
                f"(limit {MAX_TOOL_CALLS_PER_TURN}). Answer with what's already been retrieved.",
            }

        key = self._call_key(name, arguments)
        if key in self._seen_calls:
            return {
                "error": True,
                "type": "duplicate_tool_call",
                "message": f"{name} was already called with these exact arguments this turn. "
                "Reuse that earlier result instead of calling it again.",
            }

        if self._result_bytes >= MAX_TOOL_RESULT_BYTES_PER_TURN:
            return {
                "error": True,
                "type": "result_budget_exceeded",
                "message": "This turn has already retrieved a large amount of LEARN data. "
                "Answer with what's already been retrieved instead of fetching more.",
            }

        self.calls_this_turn += 1
        self._seen_calls.add(key)
        return None

    def record_result(self, result: Any) -> None:
        try:
            self._result_bytes += len(json.dumps(result))
        except TypeError:
            # Result wasn't JSON-serializable (shouldn't happen -- every
            # tool returns plain dicts/lists via agent/serialize.py) --
            # don't let a measurement failure block the turn.
            pass

    @staticmethod
    def _call_key(name: str, arguments: dict) -> tuple:
        try:
            return (name, json.dumps(arguments, sort_keys=True))
        except TypeError:
            return (name, repr(arguments))
