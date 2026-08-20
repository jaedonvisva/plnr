"""Local Ollama connection, configured entirely through environment variables.

This module only ever talks to a local Ollama server -- nothing here sends
anything off-device. A second, strictly opt-in cloud backend (Gemini) exists
in agent/gemini_client.py; see agent/backend.py for how a model id resolves
to one backend or the other, and agent.py's --model flag for how a user
picks. This module's own behavior is unchanged by that: model name and
Ollama base URL are still never hardcoded outside this module's defaults.
"""

import os

import ollama

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaUnavailableError(Exception):
    """Raised when the local Ollama server can't be reached."""


def get_model() -> str:
    return os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)


def get_base_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)


def make_client() -> ollama.Client:
    return ollama.Client(host=get_base_url())


def check_connection(client: ollama.Client | None = None, model: str | None = None) -> None:
    """Raise OllamaUnavailableError with a clear message if Ollama (or the
    configured model) isn't reachable. Call this before starting the agent
    loop so failures are diagnosed at the right layer, per the staged
    development order.

    model defaults to get_model() (the OLLAMA_MODEL env var) but can be
    overridden -- used when a user picks a specific model via agent.py's
    --model flag rather than relying on the env-var default."""
    client = client or make_client()
    base_url = get_base_url()
    model = model or get_model()

    try:
        available = {m.model for m in client.list().models}
    except Exception as exc:
        raise OllamaUnavailableError(
            f"Ollama is not available at {base_url}.\nStart Ollama and try again."
        ) from exc

    if model not in available and not any(m.startswith(model.split(":")[0] + ":") for m in available):
        raise OllamaUnavailableError(
            f"Model '{model}' is not installed in Ollama at {base_url}.\n"
            f"Available models: {sorted(available) or '(none)'}\n"
            f"Run: ollama pull {model}"
        )
