import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from agent.backend import Backend
from agent.usage import Usage
from discord_adapter.session import AgentSession, SessionState, UNAVAILABLE_MESSAGE
from learn.auth import BrowserClosedError


class UnavailableError(Exception):
    pass


def make_backend() -> Backend:
    return Backend(
        name="fake",
        model="fake-model",
        make_client=lambda: object(),
        check_connection=lambda client: None,
        unavailable_error=UnavailableError,
    )


def make_session(run_turn_fn, executor) -> AgentSession:
    return AgentSession(
        llm_client=object(),
        backend=make_backend(),
        tools={},
        executor=executor,
        run_turn_fn=run_turn_fn,
    )


def test_ask_appends_user_message_and_returns_reply():
    calls = []

    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        calls.append(list(messages))
        messages.append({"role": "assistant", "content": "the answer"})
        usage.add(10, 5)
        return "the answer"

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        reply = asyncio.run(session.ask("what's due this week?"))

    assert reply == "the answer"
    assert session.usage.total_tokens == 15
    assert calls[0][-1] == {"role": "user", "content": "what's due this week?"}


def test_ask_runs_on_the_executors_single_worker_thread_every_time():
    thread_idents = []

    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        thread_idents.append(threading.get_ident())
        return "ok"

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        asyncio.run(session.ask("first"))
        asyncio.run(session.ask("second"))

    assert len(thread_idents) == 2
    assert thread_idents[0] == thread_idents[1]
    assert thread_idents[0] != threading.get_ident()


def test_ask_transitions_to_browser_failed_on_browser_closed_error():
    call_count = 0

    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        nonlocal call_count
        call_count += 1
        raise BrowserClosedError("browser closed")

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        reply = asyncio.run(session.ask("question"))

        assert reply == UNAVAILABLE_MESSAGE
        assert session.state is SessionState.BROWSER_FAILED
        assert call_count == 1

        # A second call must short-circuit without touching run_turn_fn again.
        reply2 = asyncio.run(session.ask("another question"))
    assert reply2 == UNAVAILABLE_MESSAGE
    assert call_count == 1


def test_ask_reports_unavailable_error_without_changing_state():
    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        raise UnavailableError("temporarily down")

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        reply = asyncio.run(session.ask("question"))

    assert "temporarily unavailable" in reply
    assert session.state is SessionState.RUNNING


def test_ask_catches_unexpected_exception_and_stays_running():
    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        raise RuntimeError("boom")

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        reply = asyncio.run(session.ask("question"))

    assert "went wrong" in reply.lower()
    assert session.state is SessionState.RUNNING


def test_reset_clears_messages_and_usage():
    def fake_run_turn(llm_client, model, messages, tools, debug, usage):
        usage.add(100, 100)
        return "answer"

    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(fake_run_turn, executor)
        asyncio.run(session.ask("question"))
        assert session.usage.total_tokens == 200

        session.reset()

    assert session.usage.total_tokens == 0
    assert len(session.messages) == 1
    assert session.messages[0]["role"] == "system"


def test_status_reports_non_sensitive_fields_only():
    with ThreadPoolExecutor(max_workers=1) as executor:
        session = make_session(lambda *a, **k: "unused", executor)
        status = session.status()

    assert status == {
        "backend": "fake",
        "model": "fake-model",
        "state": "running",
        "message_count": 1,
        "total_tokens": 0,
    }
