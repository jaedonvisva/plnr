"""LEARN Agent: a local, read-only academic assistant.

    User -> LLM (Ollama/Qwen locally, or Gemini in the cloud -- see below)
         -> LEARN tool layer -> LearnClient -> Valence -> LEARN
         -> normalized result -> LLM -> User

Usage:
    python agent.py                    Full interactive agent (launches
                                        Playwright, waits for WatIAM/Duo
                                        login, then a chat loop)
    python agent.py --model qwen       Use local Ollama/Qwen instead
    python agent.py --model gemini     Use Gemini (default -- see below)
    python agent.py --model <id>       Use a specific model id directly,
                                        e.g. qwen3:8b or gemini-2.5-flash
    python agent.py logout             Clear the persisted LEARN session;
                                        you'll need to log in again next run
    python agent.py --test-llm         Test the LLM connection only, no
                                        LEARN/Playwright involved at all
    python agent.py --debug            Interactive agent with the tool-call
                                        loop printed visibly (no secrets
                                        logged)

Model selection: see agent/backend.py. IMPORTANT -- the default backend is
Gemini (cloud), not local Qwen. By default, your questions and LEARN data
(course names, grades, assignment/announcement text returned by tool calls)
are sent to Google's Gemini API. Pass `--model qwen` to keep everything
local instead; see agent/llm.py vs agent/gemini_client.py for exactly what
each backend does and does not send anywhere.
"""

import sys

from agent import backend as backend_module
from agent import triage
from agent.loop import run_turn
from agent.system_prompt import SYSTEM_PROMPT
from agent.tools import build_tools
from agent.usage import Usage
from learn import auth, browser, credentials
from learn.client import LearnClient


def _format_usage(turn: Usage, session: Usage) -> str:
    return (
        f"[tokens: {turn.prompt_tokens:,} in / {turn.completion_tokens:,} out "
        f"(turn) — {session.total_tokens:,} total this session]"
    )


def _model_arg(args: list[str]) -> str | None:
    if "--model" in args:
        i = args.index("--model")
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _connect(backend: backend_module.Backend):
    """Construct and verify an LLM client for the resolved backend, or exit
    with a clear message. Shared by run_test_llm() and run_interactive_agent()
    so both fail the same way."""
    print(f"Connecting to {backend.name} (model: {backend.model})...")
    try:
        client = backend.make_client()
        backend.check_connection(client)
    except backend.unavailable_error as exc:
        print(f"\n{exc}")
        sys.exit(1)
    print(f"{backend.model} ✓")
    return client


def run_test_llm(backend: backend_module.Backend) -> None:
    """Prove Python -> LLM works, no LEARN involved."""
    client = _connect(backend)

    question = "What is an AI agent?"
    print(f"\nYou: {question}")
    try:
        resp = client.chat(model=backend.model, messages=[{"role": "user", "content": question}], think=False)
    except backend.unavailable_error as exc:
        print(f"\n{exc}")
        sys.exit(1)
    print(f"\n{backend.name}: {resp.message.content}")
    prompt_tokens = getattr(resp, "prompt_eval_count", None)
    completion_tokens = getattr(resp, "eval_count", None)
    if prompt_tokens is not None or completion_tokens is not None:
        print(f"\n[tokens: {prompt_tokens or 0:,} in / {completion_tokens or 0:,} out]")


class AgentStartupError(Exception):
    """Raised by connect_and_authenticate() if LEARN authentication doesn't
    complete -- either the browser was closed early, or the wait timed out.
    The browser context is already closed and playwright already stopped
    before this is raised, so the caller has nothing left to clean up."""


def connect_and_authenticate(
    backend: backend_module.Backend,
) -> tuple[object, object, LearnClient, dict]:
    """Launch the browser, complete WatIAM/Duo auth (with optional
    credential auto-fill), and build a ready-to-use LearnClient + tool set.

    Returns (context, playwright, learn_client, tools). On success, the
    CALLER owns `context`/`playwright` and is responsible for calling
    context.close() and playwright.stop() when done (mirrors
    learn/browser.py::launch_browser()'s existing contract). On failure,
    raises AgentStartupError, having already closed the browser and
    stopped playwright itself -- shared by run_interactive_agent() (the
    CLI) and discord_bot.py, so this security-relevant wiring exists in
    exactly one place.
    """
    print("\nOpening LEARN...")
    context, playwright = browser.launch_browser()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(browser.LEARN_URL)

        if credentials.maybe_autofill_login(page):
            print("Filled WatIAM credentials from .env.")
            print("If Duo challenges you, approve it in the browser.")
        else:
            print("Please complete the Waterloo login and Duo authentication")
            print("in the browser.")

        try:
            authenticated = auth.wait_for_authentication(page)
        except auth.BrowserClosedError as exc:
            raise AgentStartupError(str(exc)) from exc

        if not authenticated:
            raise AgentStartupError("Timed out waiting for LEARN authentication.")

        print("LEARN authentication ✓\n")

        client = LearnClient(context)
        decider = triage.default_decider()
        if decider is not None:
            print("Announcement triage ✓ (local)\n")
        tools = build_tools(client, decider=decider)
    except Exception:
        try:
            context.close()
        except Exception:
            pass
        playwright.stop()
        raise

    return context, playwright, client, tools


def run_interactive_agent(backend: backend_module.Backend, debug: bool = False) -> None:
    """Full interactive agent, using the existing auth flow unmodified."""
    print("LEARN Agent")
    print("───────────")

    llm_client = _connect(backend)

    try:
        context, playwright, client, tools = connect_and_authenticate(backend)
    except AgentStartupError as exc:
        print(f"\n{exc}")
        return

    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        session_usage = Usage()

        print("Ask about your courses, assignments, grades, quizzes, or announcements.")
        print("Type 'exit' to quit.\n")

        while True:
            try:
                user_input = input("You: ").strip()
            except EOFError:
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                print("Goodbye.")
                break

            messages.append({"role": "user", "content": user_input})
            turn_usage = Usage()
            try:
                reply = run_turn(llm_client, backend.model, messages, tools, debug=debug, usage=turn_usage)
            except auth.BrowserClosedError:
                print("\nThe browser was closed. Restart the agent to continue.")
                break
            except backend.unavailable_error as exc:
                # Transient backend failure (e.g. Gemini's "high demand,
                # try again later" 503) -- not a bug, and not a reason to
                # kill the whole session. Your question is still the last
                # message in history; just ask again.
                print(f"\n{backend.name} is temporarily unavailable: {exc}")
                print("Try asking again.\n")
                continue
            session_usage.add_usage(turn_usage)
            print(f"\nAgent:\n{reply}\n")
            if turn_usage.total_tokens:
                print(_format_usage(turn_usage, session_usage))
    finally:
        try:
            context.close()
        except Exception:
            pass
        playwright.stop()


def run_logout() -> None:
    cleared = browser.clear_session()
    if cleared:
        print("Local session cleared. You will need to log in again next run.")
    else:
        print("No local session found. Nothing to clear.")


def main() -> None:
    args = sys.argv[1:]
    if "logout" in args:
        run_logout()
        return

    backend = backend_module.resolve(_model_arg(args))

    if "--test-llm" in args:
        run_test_llm(backend)
        return
    run_interactive_agent(backend, debug="--debug" in args)


if __name__ == "__main__":
    main()
