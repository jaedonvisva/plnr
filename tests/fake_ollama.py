"""Minimal fake for ollama.Client, so agent-loop tests run offline without a
real Ollama server or model download."""

import types


class FakeToolCall:
    def __init__(self, name: str, arguments: dict):
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeResponse:
    def __init__(self, message: FakeMessage, prompt_eval_count: int | None = None, eval_count: int | None = None):
        self.message = message
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count


class FakeOllamaClient:
    """Scripted client: each .chat() call pops the next response off a
    pre-built list. Raises if called more times than scripted."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.chat_calls: list[dict] = []

    def chat(self, model, messages, tools=None, think=None, **kwargs):
        self.chat_calls.append({"model": model, "messages": list(messages), "tools": tools})
        if not self._responses:
            raise AssertionError("FakeOllamaClient.chat() called more times than scripted")
        return self._responses.pop(0)


def tool_call_response(
    name: str, arguments: dict | None = None, prompt_eval_count: int | None = None, eval_count: int | None = None
) -> FakeResponse:
    return FakeResponse(
        FakeMessage(content="", tool_calls=[FakeToolCall(name, arguments or {})]),
        prompt_eval_count=prompt_eval_count,
        eval_count=eval_count,
    )


def final_response(content: str, prompt_eval_count: int | None = None, eval_count: int | None = None) -> FakeResponse:
    return FakeResponse(
        FakeMessage(content=content, tool_calls=None), prompt_eval_count=prompt_eval_count, eval_count=eval_count
    )
