from agent.limits import MAX_TOOL_CALLS_PER_TURN, MAX_TOOL_RESULT_BYTES_PER_TURN, TurnGuard


def test_first_call_is_allowed():
    guard = TurnGuard()
    assert guard.before_call("get_courses", {}) is None


def test_exact_duplicate_call_is_rejected():
    guard = TurnGuard()
    assert guard.before_call("search_announcements", {"query": "exam"}) is None
    result = guard.before_call("search_announcements", {"query": "exam"})
    assert result["error"] is True
    assert result["type"] == "duplicate_tool_call"


def test_same_tool_different_arguments_is_not_a_duplicate():
    guard = TurnGuard()
    assert guard.before_call("get_assignments", {"course_id": "1"}) is None
    assert guard.before_call("get_assignments", {"course_id": "2"}) is None


def test_argument_order_does_not_defeat_duplicate_detection():
    guard = TurnGuard()
    assert guard.before_call("search_announcements", {"query": "exam", "course_id": "1"}) is None
    result = guard.before_call("search_announcements", {"course_id": "1", "query": "exam"})
    assert result["type"] == "duplicate_tool_call"


def test_total_call_count_capped_per_turn():
    guard = TurnGuard()
    for i in range(MAX_TOOL_CALLS_PER_TURN):
        result = guard.before_call("get_assignments", {"course_id": str(i)})
        assert result is None
    result = guard.before_call("get_assignments", {"course_id": "one-too-many"})
    assert result["error"] is True
    assert result["type"] == "tool_call_limit_exceeded"


def test_cumulative_result_size_capped_per_turn():
    guard = TurnGuard()
    big_result = {"data": "x" * (MAX_TOOL_RESULT_BYTES_PER_TURN + 1)}
    assert guard.before_call("search_announcements", {"query": "a"}) is None
    guard.record_result(big_result)
    result = guard.before_call("search_announcements", {"query": "b"})
    assert result["error"] is True
    assert result["type"] == "result_budget_exceeded"


def test_unserializable_result_does_not_crash_record_result():
    guard = TurnGuard()

    class NotSerializable:
        pass

    guard.record_result(NotSerializable())  # must not raise
