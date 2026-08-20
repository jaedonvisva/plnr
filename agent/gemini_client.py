"""Gemini backend adapter.

This is the ONE place in the project a cloud LLM is used. It's strictly
opt-in (see agent.py's --model flag and agent/backend.py) -- the default
Ollama/Qwen path in agent/llm.py is untouched and still sends nothing
off-device.

Be clear about what changes when this backend is selected: prompts, tool
arguments, and tool results -- which include your actual LEARN data once a
tool call returns something (course names, grades, assignment text,
announcement text) -- are sent to Google's Gemini API. Nothing here hides
that; this docstring is the disclosure.

GeminiClient.chat() is written to satisfy the exact same duck-typed
interface agent/loop.py already expects from ollama.Client (see
tests/fake_ollama.py for the shape being matched: `.chat(model=, messages=,
tools=, think=) -> response` where `response.message.content` is a string
and `response.message.tool_calls[i].function.name` /
`.function.arguments` describe requested tool calls). Because of that,
agent/loop.py needed ZERO changes to support this second backend -- it was
already written against a duck-typed client, not literally `ollama.Client`.

Tool schemas are derived automatically from the plain Python callables in
agent/tools.py by the google-genai SDK itself (same idea as ollama's own
auto-schema derivation) -- nothing here hand-writes a JSON schema.
Automatic function *execution* is explicitly disabled
(AutomaticFunctionCallingConfig(disable=True)): Gemini only ever tells us
what it wants to call, agent/loop.py's _execute_tool() still does the
actual calling and error handling, identically to the Ollama path.
"""

import os
import types as pytypes
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types as genai_types

DEFAULT_MODEL = "gemini-3.1-flash-lite"
"""Chosen over gemini-3.6-flash specifically for free-tier headroom: Lite
variants get 500 requests/day (vs. 20) and 15 requests/minute (vs. 5) on
the free tier -- see the project owner's own rate-limit dashboard, hit
after ~2 real conversation turns on gemini-3.6-flash. Pass
`--model gemini-3.6-flash` explicitly if you want the non-Lite model and
have billing enabled (or are fine with the tighter free-tier caps)."""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class GeminiUnavailableError(Exception):
    """Raised when the Gemini API key is missing/invalid or the API can't be reached."""


def _load_dotenv_if_present() -> None:
    """Load GEMINI_API_KEY/GEMINI_MODEL from .env, same convention as
    learn/credentials.py's WatIAM loading. Needed here specifically because
    --test-llm and the interactive agent both reach get_api_key()/get_model()
    without ever going through credentials.py's own dotenv loading (that
    only runs inside the LEARN auth flow, which --test-llm skips entirely)."""
    if not ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ENV_FILE)


def get_model() -> str:
    _load_dotenv_if_present()
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


def get_api_key() -> str | None:
    _load_dotenv_if_present()
    return os.environ.get("GEMINI_API_KEY")


class _Message(dict):
    """A dict that also supports attribute access.

    Needs to satisfy two roles at once: agent/loop.py reads
    `response.message.content` / `.tool_calls` via attribute access (to
    match ollama.Client's response shape), and messages.append(response.message)
    puts this same object back into the `messages` history list, where
    GeminiClient._to_contents() (below) needs to recognize it as a plain
    dict on the next round-trip. Being both at once means neither caller
    needs to know which backend is in use.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _to_contents(messages: list) -> tuple[str | None, list["genai_types.Content"]]:
    """Convert the shared `messages` history (plain dicts for
    system/user/tool roles, `_Message` for prior assistant turns) into
    Gemini's (system_instruction, contents) shape. Never sees raw D2L
    JSON or credentials -- messages only ever contain what agent/tools.py
    already serialized.
    """
    import json

    system_instruction: str | None = None
    contents: list[genai_types.Content] = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")

        if role == "system":
            system_instruction = m.get("content")
        elif role == "user":
            contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=m.get("content") or "")]))
        elif role == "tool":
            raw = m.get("content")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                payload = raw
            contents.append(
                genai_types.Content(
                    role="user",
                    parts=[
                        genai_types.Part(
                            function_response=genai_types.FunctionResponse(
                                name=m.get("tool_name"), response={"result": payload}
                            )
                        )
                    ],
                )
            )
        elif role == "assistant":
            parts = []
            if m.get("content"):
                parts.append(genai_types.Part(text=m["content"]))
            for call in m.get("tool_calls") or []:
                parts.append(
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(name=call.function.name, args=call.function.arguments),
                        # Gemini 3.x models attach a thought_signature to each
                        # function-call part to preserve their reasoning trace
                        # across turns. Replaying a function call in history
                        # without it is a hard 400 ("Function call is missing
                        # a thought_signature"), not just a quality nit -- so
                        # this must round-trip through _Message untouched,
                        # not be dropped like the rest of the raw Part.
                        thought_signature=getattr(call, "thought_signature", None),
                    )
                )
            contents.append(genai_types.Content(role="model", parts=parts))

    return system_instruction, contents


class GeminiClient:
    """Wraps a real google.genai.Client, exposing an ollama.Client-shaped
    `.chat()` so agent/loop.py's run_turn() can use either backend
    unmodified."""

    def __init__(self, client: genai.Client):
        self._client = client

    def chat(self, model: str, messages: list, tools=None, think=None, **kwargs):
        system_instruction, contents = _to_contents(messages)
        tool_list = list(tools) if tools else None

        config = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tool_list,
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
        )

        try:
            response = self._client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:
            raise GeminiUnavailableError(f"Gemini request failed: {exc}") from exc

        candidates = response.candidates or []
        parts = candidates[0].content.parts if candidates and candidates[0].content else []

        text_parts: list[str] = []
        tool_calls: list[Any] = []
        for part in parts or []:
            if getattr(part, "text", None):
                text_parts.append(part.text)
            if getattr(part, "function_call", None):
                fc = part.function_call
                tool_calls.append(
                    pytypes.SimpleNamespace(
                        function=pytypes.SimpleNamespace(name=fc.name, arguments=dict(fc.args or {})),
                        # See _to_contents()'s "assistant" branch: this must
                        # survive the round-trip through `messages` history
                        # and get reattached when this call is replayed back
                        # to the API, or Gemini 3.x rejects the next request.
                        thought_signature=getattr(part, "thought_signature", None),
                    )
                )

        message = _Message(
            role="assistant",
            content="".join(text_parts),
            tool_calls=tool_calls or None,
        )
        usage = getattr(response, "usage_metadata", None)
        completion_tokens = None
        if usage:
            # candidates_token_count (visible answer) + thoughts_token_count
            # (Gemini 3.x's internal reasoning, billed as output too) --
            # NOT + tool_use_prompt_token_count, which is input-side (it's
            # already counted again in prompt_token_count on the next round
            # via the resent history; adding it here would double-count it).
            completion_tokens = (getattr(usage, "candidates_token_count", None) or 0) + (
                getattr(usage, "thoughts_token_count", None) or 0
            )
        return pytypes.SimpleNamespace(
            message=message,
            # Same field names ollama.Client's response already carries
            # natively (see agent/usage.py) -- so agent/loop.py's usage
            # tracking works identically for both backends without knowing
            # which one is in use.
            prompt_eval_count=getattr(usage, "prompt_token_count", None) if usage else None,
            eval_count=completion_tokens,
        )

    def list(self):
        """Mirrors ollama.Client.list()'s shape closely enough for
        check_connection() below to reuse the same "is my model available"
        pattern as the Ollama path."""
        models = self._client.models.list()
        return pytypes.SimpleNamespace(
            models=[pytypes.SimpleNamespace(model=m.name.removeprefix("models/")) for m in models]
        )


def make_client() -> GeminiClient:
    api_key = get_api_key()
    if not api_key:
        raise GeminiUnavailableError(
            "GEMINI_API_KEY is not set. Add it to your .env file to use --model gemini."
        )
    return GeminiClient(genai.Client(api_key=api_key))


def check_connection(client: GeminiClient | None = None, model: str | None = None) -> None:
    """Raise GeminiUnavailableError with a clear message if the API key is
    missing/invalid or the configured model isn't available. Mirrors
    agent/llm.py's check_connection() -- same shape, different backend."""
    client = client or make_client()
    model = model or get_model()

    try:
        available = {m.model for m in client.list().models}
    except GeminiUnavailableError:
        raise
    except Exception as exc:
        raise GeminiUnavailableError(f"Could not reach the Gemini API: {exc}") from exc

    if model not in available:
        raise GeminiUnavailableError(
            f"Model '{model}' is not available for this API key.\n"
            f"Available models: {sorted(available) or '(none)'}"
        )
