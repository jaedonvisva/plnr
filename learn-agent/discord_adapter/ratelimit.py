"""Persistent, cross-message rate limiting for the Discord adapter.

Deliberately separate from agent/limits.py's TurnGuard: TurnGuard bounds
tool-call abuse WITHIN one turn (defense-in-depth against a runaway or
prompt-injected tool loop). This bounds request volume ACROSS many turns
over minutes/hours, for an always-listening transport where "who's asking"
and "how many times have they asked recently" now actually exist -- exactly
the layer TurnGuard's own module docstring said would need new plumbing
once a real caller with a notion of sender showed up. Not primarily about
the single authorized user abusing their own bot -- it's a safety net if
the bot token leaks or the Discord account is compromised.
"""

import time
from collections.abc import Callable


class RateLimiter:
    """Sliding-window limiter over the last minute and the last hour.
    `clock` is injectable so tests don't need to sleep for real."""

    def __init__(
        self,
        max_per_minute: int,
        max_per_hour: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max_per_minute = max_per_minute
        self._max_per_hour = max_per_hour
        self._clock = clock
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        now = self._clock()
        self._timestamps = [t for t in self._timestamps if now - t < 3600]

        per_minute_count = sum(1 for t in self._timestamps if now - t < 60)
        if per_minute_count >= self._max_per_minute:
            return False
        if len(self._timestamps) >= self._max_per_hour:
            return False

        self._timestamps.append(now)
        return True
