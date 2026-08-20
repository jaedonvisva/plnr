import json

from agent.gemini_client import GeminiClient, GeminiUnavailableError, check_connection, get_model
from agent.loop import run_turn
from tests.fake_gemini import FakeGenaiClient, final_response, tool_call_response


def get_courses():
    """get courses"""
    return []


def get_assignments(course_id):
    """get assignments"""
    return []


def test_chat_no_tool_call_returns_text_content():
    sdk = FakeGenaiClient(responses=[final_response("plain answer")])
    client = GeminiClient(sdk)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    response = client.chat(model="gemini-2.5-flash", messages=messages, tools=[])
    assert response.message.content == "plain answer"
    assert response.message.tool_calls is None


def test_chat_exposes_token_usage_in_ollama_field_names():
    """agent/loop.py's usage tracking reads response.prompt_eval_count /
    .eval_count regardless of backend (see agent/usage.py) -- GeminiClient
    must populate those exact names from Gemini's own usage_metadata.
    completion == candidates + thoughts (both billed as output; see
    agent/gemini_client.py for why tool_use_prompt_token_count is excluded)."""
    sdk = FakeGenaiClient(
        responses=[final_response("done", prompt_tokens=1000, candidates_tokens=50, thoughts_tokens=30)]
    )
    client = GeminiClient(sdk)
    messages = [{"role": "user", "content": "hi"}]

    response = client.chat(model="gemini-3.1-flash-lite", messages=messages, tools=[])
    assert response.prompt_eval_count == 1000
    assert response.eval_count == 80


def test_chat_usage_none_when_response_has_no_usage_metadata():
    """Must not crash if a response doesn't report usage."""
    sdk = FakeGenaiClient(responses=[final_response("done")])
    client = GeminiClient(sdk)
    messages = [{"role": "user", "content": "hi"}]

    response = client.chat(model="gemini-3.1-flash-lite", messages=messages, tools=[])
    assert response.prompt_eval_count is None
    assert response.eval_count is None


def test_chat_tool_call_response_shape_matches_ollama_client():
    """The whole point of this adapter: response.message.tool_calls[i].function.name
    / .arguments must work exactly like ollama's response shape, since
    agent/loop.py reads it that way regardless of backend."""
    sdk = FakeGenaiClient(responses=[tool_call_response("get_courses", {})])
    client = GeminiClient(sdk)
    messages = [{"role": "user", "content": "hi"}]

    response = client.chat(model="gemini-2.5-flash", messages=messages, tools=[get_courses])
    assert response.message.content == ""
    call = response.message.tool_calls[0]
    assert call.function.name == "get_courses"
    assert call.function.arguments == {}


def test_chat_round_trips_thought_signature_on_replayed_function_call():
    """Regression test for a live-observed 400: Gemini 3.x models attach a
    thought_signature to function-call parts and reject a follow-up request
    that replays the call without it ("Function call is missing a
    thought_signature in functionCall parts"). GeminiClient must carry it
    from the response through `messages` history and back onto the request.
    """
    sdk = FakeGenaiClient(
        responses=[
            tool_call_response("get_courses", thought_signature=b"sig-123"),
            final_response("done"),
        ]
    )
    client = GeminiClient(sdk)
    messages = [{"role": "user", "content": "hi"}]

    response1 = client.chat(model="gemini-3.6-flash", messages=messages, tools=[get_courses])
    messages.append(response1.message)
    messages.append({"role": "tool", "content": "[]", "tool_name": "get_courses"})

    client.chat(model="gemini-3.6-flash", messages=messages, tools=[get_courses])

    second_call_contents = sdk.models.generate_content_calls[1]["contents"]
    replayed_parts = [p for c in second_call_contents for p in (c.parts or [])]
    function_call_parts = [p for p in replayed_parts if p.function_call is not None]
    assert len(function_call_parts) == 1
    assert function_call_parts[0].thought_signature == b"sig-123"


def test_chat_extracts_system_message_as_system_instruction():
    sdk = FakeGenaiClient(responses=[final_response("ok")])
    client = GeminiClient(sdk)
    messages = [{"role": "system", "content": "be concise"}, {"role": "user", "content": "hi"}]

    client.chat(model="gemini-2.5-flash", messages=messages, tools=[])
    config = sdk.models.generate_content_calls[0]["config"]
    assert config.system_instruction == "be concise"
    # The system message must not also appear as a regular content turn.
    assert len(sdk.models.generate_content_calls[0]["contents"]) == 1


def test_run_turn_works_end_to_end_with_gemini_backend():
    """Proves agent/loop.py's run_turn() needed zero changes to support this
    backend -- same multi-round tool-calling test as
    tests/test_agent_loop.py::test_multiple_tool_calls_across_rounds, but
    against GeminiClient instead of FakeOllamaClient."""
    sdk = FakeGenaiClient(
        responses=[
            tool_call_response("get_courses"),
            tool_call_response("get_assignments", {"course_id": "42"}),
            final_response("Assignment 1 is due 2026-08-20."),
        ]
    )
    client = GeminiClient(sdk)

    seen_course_id = []

    def _get_courses():
        return [{"course_id": "42", "code": "MSE 240"}]

    def _get_assignments(course_id):
        seen_course_id.append(course_id)
        return [{"name": "Assignment 1", "due_at": "2026-08-20"}]

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    result = run_turn(
        client, "gemini-2.5-flash", messages, {"get_courses": _get_courses, "get_assignments": _get_assignments}
    )
    assert result == "Assignment 1 is due 2026-08-20."
    assert seen_course_id == ["42"]

    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert json.loads(tool_msgs[1]["content"]) == [{"name": "Assignment 1", "due_at": "2026-08-20"}]


def test_run_turn_error_handling_identical_to_ollama_path():
    """Tool errors must still become structured dicts, not crash -- proves
    _execute_tool()'s error handling in agent/loop.py is backend-agnostic."""
    from learn.exceptions import LearnAuthenticationError

    def _get_assignments(course_id):
        raise LearnAuthenticationError("expired")

    sdk = FakeGenaiClient(
        responses=[tool_call_response("get_assignments", {"course_id": "1"}), final_response("session expired")]
    )
    client = GeminiClient(sdk)
    messages = [{"role": "user", "content": "hi"}]
    run_turn(client, "gemini-2.5-flash", messages, {"get_assignments": _get_assignments})

    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload == {
        "error": True,
        "type": "authentication_expired",
        "message": "Your LEARN session has expired. Please restart and authenticate again.",
    }


# ---- get_model / check_connection -----------------------------------------

def test_get_model_default(monkeypatch, tmp_path):
    from agent import gemini_client

    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(gemini_client, "ENV_FILE", tmp_path / "nonexistent.env")
    assert get_model() == "gemini-3.1-flash-lite"


def test_get_model_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom")
    assert get_model() == "gemini-custom"


def test_check_connection_raises_when_model_missing():
    sdk = FakeGenaiClient(list_models=["gemini-2.0-flash"])
    client = GeminiClient(sdk)
    try:
        check_connection(client, model="gemini-2.5-flash")
        assert False, "expected GeminiUnavailableError"
    except GeminiUnavailableError:
        pass


def test_check_connection_passes_when_model_present():
    sdk = FakeGenaiClient(list_models=["gemini-2.5-flash"])
    client = GeminiClient(sdk)
    check_connection(client, model="gemini-2.5-flash")  # should not raise


def test_check_connection_raises_when_api_unreachable():
    sdk = FakeGenaiClient()
    sdk.models.list = lambda: (_ for _ in ()).throw(ConnectionError("down"))
    client = GeminiClient(sdk)
    try:
        check_connection(client, model="gemini-2.5-flash")
        assert False, "expected GeminiUnavailableError"
    except GeminiUnavailableError:
        pass


def test_make_client_raises_when_api_key_missing(monkeypatch, tmp_path):
    from agent import gemini_client
    from agent.gemini_client import make_client

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # get_api_key() reloads .env on every call (needed so --test-llm sees a
    # real .env without going through credentials.py's own loading) -- point
    # it at a nonexistent file so a real developer .env can't leak the key
    # straight back into os.environ and defeat this test.
    monkeypatch.setattr(gemini_client, "ENV_FILE", tmp_path / "nonexistent.env")
    try:
        make_client()
        assert False, "expected GeminiUnavailableError"
    except GeminiUnavailableError as exc:
        assert "GEMINI_API_KEY" in str(exc)
