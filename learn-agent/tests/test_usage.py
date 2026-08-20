from agent.usage import Usage


def test_add_accumulates_and_treats_none_as_zero():
    u = Usage()
    u.add(100, 20)
    u.add(None, 5)
    u.add(50, None)
    assert u.prompt_tokens == 150
    assert u.completion_tokens == 25
    assert u.total_tokens == 175


def test_add_usage_merges_two_accumulators():
    turn = Usage(prompt_tokens=100, completion_tokens=10)
    session = Usage(prompt_tokens=500, completion_tokens=50)
    session.add_usage(turn)
    assert session.prompt_tokens == 600
    assert session.completion_tokens == 60


def test_fresh_usage_is_zero():
    u = Usage()
    assert u.total_tokens == 0
