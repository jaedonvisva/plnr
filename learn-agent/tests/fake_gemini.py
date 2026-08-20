"""Minimal fake for google.genai.Client, so GeminiClient tests run offline
without a real Gemini API key or network access. Mirrors tests/fake_ollama.py's
approach for the Ollama side."""

import types

from google.genai import types as genai_types


class FakeModelsAPI:
    def __init__(self, responses: list, list_models: list[str] | None = None, raise_on_generate=None):
        self._responses = list(responses)
        self.generate_content_calls: list[dict] = []
        self._list_models = list_models or []
        self._raise_on_generate = raise_on_generate

    def generate_content(self, *, model, contents, config=None):
        self.generate_content_calls.append({"model": model, "contents": contents, "config": config})
        if self._raise_on_generate:
            raise self._raise_on_generate
        if not self._responses:
            raise AssertionError("FakeModelsAPI.generate_content() called more times than scripted")
        return self._responses.pop(0)

    def list(self):
        return [types.SimpleNamespace(name=f"models/{m}") for m in self._list_models]


class FakeGenaiClient:
    """Stands in for google.genai.Client."""

    def __init__(self, responses=None, list_models=None, raise_on_generate=None):
        self.models = FakeModelsAPI(responses or [], list_models=list_models, raise_on_generate=raise_on_generate)


def fake_response(
    text: str = "",
    function_calls: list[tuple[str, dict]] | None = None,
    thought_signature: bytes | None = None,
    prompt_tokens: int | None = None,
    candidates_tokens: int | None = None,
    thoughts_tokens: int | None = None,
):
    """Build a fake google.genai GenerateContentResponse-shaped object with
    the exact attributes GeminiClient.chat() reads: .candidates[0].content.parts,
    each part's .text / .function_call.name / .function_call.args /
    .thought_signature, and .usage_metadata. thought_signature mimics what
    Gemini 3.x models attach to function-call parts (see
    agent/gemini_client.py) -- pass it to test that GeminiClient round-trips
    it back on the next request. prompt_tokens/candidates_tokens/thoughts_tokens
    mimic Gemini's real usage_metadata fields; leave all None to mimic a
    response that doesn't report usage (GeminiClient must tolerate that)."""
    parts = []
    if text:
        parts.append(genai_types.Part(text=text))
    for name, args in function_calls or []:
        parts.append(
            genai_types.Part(
                function_call=genai_types.FunctionCall(name=name, args=args),
                thought_signature=thought_signature,
            )
        )
    content = genai_types.Content(role="model", parts=parts)
    candidate = types.SimpleNamespace(content=content)

    usage_metadata = None
    if prompt_tokens is not None or candidates_tokens is not None or thoughts_tokens is not None:
        usage_metadata = types.SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
            thoughts_token_count=thoughts_tokens,
        )

    return types.SimpleNamespace(candidates=[candidate], usage_metadata=usage_metadata)


def tool_call_response(
    name: str,
    args: dict | None = None,
    thought_signature: bytes | None = None,
    prompt_tokens: int | None = None,
    candidates_tokens: int | None = None,
    thoughts_tokens: int | None = None,
):
    return fake_response(
        function_calls=[(name, args or {})],
        thought_signature=thought_signature,
        prompt_tokens=prompt_tokens,
        candidates_tokens=candidates_tokens,
        thoughts_tokens=thoughts_tokens,
    )


def final_response(
    text: str,
    prompt_tokens: int | None = None,
    candidates_tokens: int | None = None,
    thoughts_tokens: int | None = None,
):
    return fake_response(
        text=text, prompt_tokens=prompt_tokens, candidates_tokens=candidates_tokens, thoughts_tokens=thoughts_tokens
    )
