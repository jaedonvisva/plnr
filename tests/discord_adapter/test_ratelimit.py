from discord_adapter.ratelimit import RateLimiter


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_per_minute_limit():
    clock = FakeClock()
    limiter = RateLimiter(max_per_minute=3, max_per_hour=100, clock=clock)

    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False


def test_per_minute_limit_resets_after_60_seconds():
    clock = FakeClock()
    limiter = RateLimiter(max_per_minute=1, max_per_hour=100, clock=clock)

    assert limiter.allow() is True
    assert limiter.allow() is False

    clock.advance(61)
    assert limiter.allow() is True


def test_allows_up_to_per_hour_limit_even_with_minute_gaps():
    clock = FakeClock()
    limiter = RateLimiter(max_per_minute=1, max_per_hour=2, clock=clock)

    assert limiter.allow() is True
    clock.advance(61)
    assert limiter.allow() is True
    clock.advance(61)
    assert limiter.allow() is False


def test_per_hour_limit_resets_after_3600_seconds():
    clock = FakeClock()
    limiter = RateLimiter(max_per_minute=100, max_per_hour=1, clock=clock)

    assert limiter.allow() is True
    assert limiter.allow() is False

    clock.advance(3601)
    assert limiter.allow() is True
