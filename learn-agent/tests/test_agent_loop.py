import json

import pytest

from agent.loop import MAX_TOOL_ROUNDS, run_turn
from agent.usage import Usage
from learn.exceptions import LearnApiError, LearnAuthenticationError
from tests.fake_ollama import FakeOllamaClient, final_response, tool_call_response


def run(responses, tools):
    client = FakeOllamaClient(responses)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    result = run_turn(client, "qwen3:8b", messages, tools, debug=False)
    return result, messages, client


def test_no_tool_call_returns_content_directly():
    result, messages, client = run([final_response("just an answer")], tools={})
    assert result == "just an answer"
    assert len(client.chat_calls) == 1


def test_single_tool_call_then_final_answer():
    calls = []

    def get_courses():
        calls.append("called")
        return [{"course_id": "1", "code": "MSE 240"}]

    result, messages, client = run(
        [tool_call_response("get_courses"), final_response("You take MSE 240.")],
        tools={"get_courses": get_courses},
    )
    assert result == "You take MSE 240."
    assert calls == ["called"]
    # A tool-result message should have been appended with the JSON payload.
    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"]) == [{"course_id": "1", "code": "MSE 240"}]
    assert tool_msgs[0]["tool_name"] == "get_courses"


def test_multiple_tool_calls_across_rounds():
    """Proves the loop supports the get_courses() -> get_assignments(id) ->
    final-answer flow across separate rounds, without hard-coding it."""
    seen_course_id = []

    def get_courses():
        return [{"course_id": "42", "code": "MSE 240"}]

    def get_assignments(course_id):
        seen_course_id.append(course_id)
        return [{"name": "Assignment 1", "due_at": "2026-08-20"}]

    result, messages, client = run(
        [
            tool_call_response("get_courses"),
            tool_call_response("get_assignments", {"course_id": "42"}),
            final_response("Assignment 1 is due 2026-08-20."),
        ],
        tools={"get_courses": get_courses, "get_assignments": get_assignments},
    )
    assert result == "Assignment 1 is due 2026-08-20."
    assert seen_course_id == ["42"]
    assert len(client.chat_calls) == 3


def test_unknown_tool_returns_error_not_crash():
    result, messages, client = run(
        [tool_call_response("delete_everything"), final_response("I can't do that.")],
        tools={},
    )
    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["error"] is True
    assert payload["type"] == "unknown_tool"


def test_authentication_error_becomes_typed_error_not_empty_list():
    def get_assignments(course_id):
        raise LearnAuthenticationError("expired")

    result, messages, client = run(
        [tool_call_response("get_assignments", {"course_id": "1"}), final_response("session expired")],
        tools={"get_assignments": get_assignments},
    )
    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload == {
        "error": True,
        "type": "authentication_expired",
        "message": "Your LEARN session has expired. Please restart and authenticate again.",
    }
    # Critically: NOT an empty list, which would look like "no assignments".
    assert payload != []


def test_api_error_becomes_typed_error():
    def get_grades(course_id):
        raise LearnApiError("boom", status=500)

    result, messages, client = run(
        [tool_call_response("get_grades", {"course_id": "1"}), final_response("api error")],
        tools={"get_grades": get_grades},
    )
    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["error"] is True
    assert payload["type"] == "api_error"


def test_arbitrary_tool_exception_becomes_typed_error_not_crash():
    def get_quizzes(course_id):
        raise RuntimeError("unexpected")

    result, messages, client = run(
        [tool_call_response("get_quizzes", {"course_id": "1"}), final_response("tool failed")],
        tools={"get_quizzes": get_quizzes},
    )
    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["error"] is True
    assert payload["type"] == "tool_error"


def test_empty_result_list_is_not_treated_as_error():
    def get_announcements(course_id):
        return []

    result, messages, client = run(
        [tool_call_response("get_announcements", {"course_id": "1"}), final_response("no announcements")],
        tools={"get_announcements": get_announcements},
    )
    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload == []


def test_max_tool_rounds_guard_terminates():
    def get_courses():
        return [{"course_id": "1"}]

    responses = [tool_call_response("get_courses") for _ in range(MAX_TOOL_ROUNDS + 2)]
    client = FakeOllamaClient(responses)
    messages = [{"role": "user", "content": "hi"}]
    result = run_turn(client, "qwen3:8b", messages, {"get_courses": get_courses}, debug=False)
    assert "wasn't able" in result.lower()
    assert len(client.chat_calls) == MAX_TOOL_ROUNDS


def test_debug_mode_does_not_print_raw_result_only_summary(capsys):
    def get_courses():
        return [{"course_id": "1", "code": "MSE 240", "secret_should_not_print": "SENTINEL_VALUE"}]

    run(
        [tool_call_response("get_courses"), final_response("done")],
        tools={"get_courses": get_courses},
    )
    # Re-run with debug=True to capture output.
    client = FakeOllamaClient([tool_call_response("get_courses"), final_response("done")])
    messages = [{"role": "user", "content": "hi"}]
    run_turn(client, "qwen3:8b", messages, {"get_courses": get_courses}, debug=True)
    out = capsys.readouterr().out
    assert "SENTINEL_VALUE" not in out
    assert "Agent → get_courses" in out
    assert "Tool → returned 1 item(s)" in out


def test_usage_accumulates_across_rounds_when_tracker_given():
    def get_courses():
        return [{"course_id": "1"}]

    client = FakeOllamaClient(
        [
            tool_call_response("get_courses", prompt_eval_count=500, eval_count=20),
            final_response("done", prompt_eval_count=800, eval_count=40),
        ]
    )
    messages = [{"role": "user", "content": "hi"}]
    usage = Usage()
    run_turn(client, "qwen3:8b", messages, {"get_courses": get_courses}, usage=usage)

    assert usage.prompt_tokens == 1300
    assert usage.completion_tokens == 60
    assert usage.total_tokens == 1360


def test_usage_not_tracked_when_no_tracker_given():
    """The default (usage=None) must not crash -- most callers don't care."""
    result, messages, client = run([final_response("just an answer")], tools={})
    assert result == "just an answer"
