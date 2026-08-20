"""The tool-calling agent loop.

    messages = [system_prompt, ...conversation, user_message]
    while True:
        response = qwen(messages, tools=learn_tools)
        if response has tool calls:
            execute each tool, append results, continue
        else:
            return response

Qwen decides which tools to call and in what order/how many times -- this
module never hard-codes a sequence like "get_courses then get_assignments".
"""

import json
from collections.abc import Callable
from typing import Any

import ollama

from learn.exceptions import LearnApiError, LearnAuthenticationError

from .limits import MAX_TOOL_CALLS_PER_ROUND, TurnGuard
from .usage import Usage

MAX_TOOL_ROUNDS = 20  # guards against a runaway tool-call loop


def _execute_tool(tools: dict[str, Callable], name: str, arguments: dict) -> Any:
    """Run one tool call, turning any failure into a structured error dict
    instead of letting it crash the loop or silently look like empty data.
    """
    fn = tools.get(name)
    if fn is None:
        return {"error": True, "type": "unknown_tool", "message": f"No such tool: {name}"}

    try:
        return fn(**arguments)
    except LearnAuthenticationError:
        return {
            "error": True,
            "type": "authentication_expired",
            "message": "Your LEARN session has expired. Please restart and authenticate again.",
        }
    except LearnApiError as exc:
        return {"error": True, "type": "api_error", "message": str(exc)}
    except TypeError as exc:
        # Wrong/missing arguments from the model -- report clearly rather
        # than crashing the whole conversation.
        return {"error": True, "type": "tool_error", "message": f"Invalid arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any tool failure must become a reportable result, not a crash
        return {"error": True, "type": "tool_error", "message": f"{name} failed: {exc}"}


def run_turn(
    ollama_client: ollama.Client,
    model: str,
    messages: list[dict],
    tools: dict[str, Callable],
    debug: bool = False,
    usage: Usage | None = None,
) -> str:
    """Run one full agent turn (possibly multiple tool-call rounds) and
    return the final natural-language reply. `messages` is mutated in
    place with everything that happened, so the caller can keep it as
    ongoing conversation history.

    usage: optional Usage accumulator. If given, every round's token counts
    (read from `response.prompt_eval_count`/`.eval_count` -- populated
    natively by ollama.Client, and by GeminiClient from Gemini's own
    usage_metadata; see agent/usage.py) are added to it. The return type
    stays `str` regardless -- pass a fresh Usage() in and inspect it after
    the call rather than changing what this function returns.

    Every tool call in this turn also passes through a fresh TurnGuard (see
    agent/limits.py): a hard cap on tool calls per round, total calls per
    turn, exact-duplicate calls, and cumulative result size. This is
    independent of MAX_TOOL_ROUNDS -- defense-in-depth against a runaway or
    adversarially-triggered tool loop (e.g. LEARN content that tries to
    prompt-inject repeated tool calls), not just a round-count cap."""
    tool_list = list(tools.values())
    guard = TurnGuard()

    for _ in range(MAX_TOOL_ROUNDS):
        response = ollama_client.chat(model=model, messages=messages, tools=tool_list, think=False)
        if usage is not None:
            usage.add(getattr(response, "prompt_eval_count", None), getattr(response, "eval_count", None))
        messages.append(response.message)

        if not response.message.tool_calls:
            return response.message.content or ""

        for i, call in enumerate(response.message.tool_calls):
            name = call.function.name
            arguments = call.function.arguments or {}
            if debug:
                print(f"Agent → {name}({arguments})")

            if i >= MAX_TOOL_CALLS_PER_ROUND:
                result = {
                    "error": True,
                    "type": "tool_call_limit_exceeded",
                    "message": f"Too many tool calls requested in one round (limit {MAX_TOOL_CALLS_PER_ROUND}).",
                }
            else:
                guard_error = guard.before_call(name, arguments)
                result = guard_error if guard_error is not None else _execute_tool(tools, name, arguments)
                guard.record_result(result)

            if debug:
                if isinstance(result, dict) and result.get("error"):
                    print(f"Tool → error: {result['message']}")
                elif isinstance(result, list):
                    print(f"Tool → returned {len(result)} item(s)")
                else:
                    print("Tool → returned result")

            messages.append({"role": "tool", "content": json.dumps(result), "tool_name": name})

    return "I wasn't able to finish gathering that information -- please try rephrasing your question."
