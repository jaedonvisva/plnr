from agent import backend
from agent.gemini_client import GeminiUnavailableError
from agent.llm import OllamaUnavailableError


def test_default_backend_is_gemini(monkeypatch):
    """Confirms the explicitly-requested default: omitting --model uses
    Gemini (cloud), not local Qwen."""
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    b = backend.resolve(None)
    assert b.name == "gemini"
    assert b.model == "gemini-3.1-flash-lite"
    assert b.unavailable_error is GeminiUnavailableError


def test_gemini_alias():
    b = backend.resolve("gemini")
    assert b.name == "gemini"


def test_qwen_alias_uses_ollama_get_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "custom:1b")
    b = backend.resolve("qwen")
    assert b.name == "ollama"
    assert b.model == "custom:1b"
    assert b.unavailable_error is OllamaUnavailableError


def test_ollama_alias_same_as_qwen_alias(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    b = backend.resolve("ollama")
    assert b.name == "ollama"
    assert b.model == "qwen3:8b"


def test_literal_gemini_model_id_routed_to_gemini():
    b = backend.resolve("gemini-2.0-flash-exp")
    assert b.name == "gemini"
    assert b.model == "gemini-2.0-flash-exp"


def test_literal_non_gemini_model_id_routed_to_ollama():
    b = backend.resolve("llama3:8b")
    assert b.name == "ollama"
    assert b.model == "llama3:8b"


def test_backend_check_connection_passes_resolved_model_not_env_default(monkeypatch):
    """A literal --model value must be what's actually checked/used, not
    silently overridden by OLLAMA_MODEL/GEMINI_MODEL env defaults."""
    monkeypatch.setenv("OLLAMA_MODEL", "some-other-default:1b")
    b = backend.resolve("llama3:8b")

    class FakeClient:
        def list(self):
            import types

            return types.SimpleNamespace(models=[types.SimpleNamespace(model="llama3:8b")])

    b.check_connection(FakeClient())  # should not raise -- checks "llama3:8b", not the env default
