import types

import pytest

from agent.llm import OllamaUnavailableError, check_connection, get_base_url, get_model


class FakeModelEntry:
    def __init__(self, model: str):
        self.model = model


class FakeListResponse:
    def __init__(self, models: list[str]):
        self.models = [FakeModelEntry(m) for m in models]


class FakeOllamaAdminClient:
    def __init__(self, models: list[str] | None = None, raise_on_list: bool = False):
        self._models = models or []
        self._raise_on_list = raise_on_list

    def list(self):
        if self._raise_on_list:
            raise ConnectionError("server not running")
        return FakeListResponse(self._models)


def test_get_model_and_base_url_defaults(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert get_model() == "qwen3:8b"
    assert get_base_url() == "http://localhost:11434"


def test_get_model_and_base_url_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "custom:1b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example.internal:9999")
    assert get_model() == "custom:1b"
    assert get_base_url() == "http://example.internal:9999"


def test_check_connection_raises_when_server_unreachable():
    client = FakeOllamaAdminClient(raise_on_list=True)
    with pytest.raises(OllamaUnavailableError):
        check_connection(client)


def test_check_connection_passes_when_model_present(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    client = FakeOllamaAdminClient(models=["qwen3:8b"])
    check_connection(client)  # should not raise


def test_check_connection_raises_when_model_missing(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    client = FakeOllamaAdminClient(models=["llama3:8b"])
    with pytest.raises(OllamaUnavailableError):
        check_connection(client)
