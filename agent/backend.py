"""Resolves a --model value (from agent.py's CLI) to a backend: local
Ollama/Qwen or cloud Gemini. This is the single place that decision gets
made, so agent.py doesn't need to know backend details itself.

DEFAULT_MODEL is Gemini's default model -- Gemini is the default backend
when --model is omitted entirely. That means, by default, prompts and tool
results (including your actual LEARN data -- course names, grades,
assignment/announcement text) are sent to Google's Gemini API. Pass
`--model qwen` (or any Ollama model id) to keep everything local instead;
see agent/llm.py and agent/gemini_client.py for what each backend actually
does.

Recognized --model values:
  "qwen" / "ollama"  -> Ollama backend, model = agent.llm.get_model()
                         (OLLAMA_MODEL env var, default "qwen3:8b")
  "gemini"           -> Gemini backend, model = gemini_client.DEFAULT_MODEL
  anything else      -> treated as a literal model id; routed to Gemini if
                         it starts with "gemini", otherwise to Ollama (so a
                         specific Gemini variant or a custom/fine-tuned
                         Ollama tag both still work)
  omitted / None     -> same as "gemini" (see DEFAULT_MODEL above)
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import gemini_client, llm

DEFAULT_MODEL = gemini_client.DEFAULT_MODEL


@dataclass
class Backend:
    name: str
    """"ollama" or "gemini" -- for display/logging only."""
    model: str
    make_client: Callable[[], Any]
    check_connection: Callable[[Any], None]
    unavailable_error: type[Exception]


def resolve(model_arg: str | None) -> Backend:
    """Resolve a --model CLI value into a concrete Backend."""
    if model_arg is None or model_arg == "gemini":
        model = gemini_client.get_model()
        is_gemini = True
    elif model_arg in ("qwen", "ollama"):
        model = llm.get_model()
        is_gemini = False
    else:
        model = model_arg
        is_gemini = model.startswith("gemini")

    if is_gemini:
        return Backend(
            name="gemini",
            model=model,
            make_client=gemini_client.make_client,
            check_connection=lambda client: gemini_client.check_connection(client, model=model),
            unavailable_error=gemini_client.GeminiUnavailableError,
        )
    return Backend(
        name="ollama",
        model=model,
        make_client=llm.make_client,
        check_connection=lambda client: llm.check_connection(client, model=model),
        unavailable_error=llm.OllamaUnavailableError,
    )
