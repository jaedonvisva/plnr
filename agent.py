"""LEARN Agent: a local, read-only academic assistant.

    User -> LLM (Ollama/Qwen locally, or Gemini in the cloud -- see below)
         -> LEARN tool layer -> LearnClient -> Valence -> LEARN
         -> normalized result -> LLM -> User

Usage:
    python agent.py                    Full interactive agent (reuses the saved
                                        LEARN session, or opens a dedicated
                                        Chrome/Edge window for WatIAM/Duo
                                        login, then a chat loop)
    python agent.py --model qwen       Use local Ollama/Qwen instead
    python agent.py --model gemini     Use Gemini (default -- see below)
    python agent.py --model <id>       Use a specific model id directly,
                                        e.g. qwen3:8b or gemini-2.5-flash
    python agent.py auth               Sign in (dedicated browser) and save the session
    python agent.py auth --check       Verify the saved session without a browser;
                                        exits 75 if missing/expired
    python agent.py auth --paste       Sign in by pasting cookies (hidden input) when
                                        no browser/display is available
    python agent.py auth --push user@host [--dir ~/plnr] [--python .venv/bin/python]
                                        Send your saved session to a server over ssh
                                        (runs `auth --stdin` there; cookies go via stdin only)
    python agent.py auth --export [--stdout]
                                        Copy the session cookies to the clipboard (or print)
    python agent.py auth --stdin       Read cookies from piped stdin (what --push runs)
    python agent.py auth --clear-profile
                                        Remove the saved session and the dedicated
                                        browser profile (resets Duo trust)
    python agent.py logout             Clear the saved session (keeps the browser
                                        profile, so Duo stays trusted)
    python agent.py today [--days N] [--ics FILE]
                                        Deterministic "what's due" view (no LLM);
                                        optionally write a deadlines .ics file
    python agent.py diff [--no-save]   What changed in LEARN since the last check
                                        (new/removed items, moved due dates, grades)
    python agent.py doctor             Diagnose setup problems; names one next step
    python agent.py install-service [--model qwen] [--load]
                                        Install a macOS LaunchAgent that keeps the
                                        proactive Discord bot running (see README);
                                        --load also starts it now
    python agent.py uninstall-service  Stop it and remove the LaunchAgent
    python agent.py --test-llm         Test the LLM connection only, no
                                        LEARN involved at all
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
from datetime import datetime, timezone
from pathlib import Path

from agent import backend as backend_module
from agent import changes, doctor, service, today as today_view, triage
from agent.loop import run_turn
from agent.system_prompt import SYSTEM_PROMPT
from agent.tools import build_tools
from agent.usage import Usage
from learn import auth, session as learn_session, transfer
from learn.exceptions import LearnApiError, LoginError
from learn.client import LearnClient
from learn.transport import SessionTransport


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
    complete -- sign-in failed, was stopped at an undeclared host, or timed out."""


def obtain_client(*, reauth: bool = True, allow_login: bool = True) -> LearnClient:
    """A LearnClient on a verified session: the saved one, else a fresh sign-in.

    reauth=True (the CLI): if the session expires later, the transport re-runs sign-in once
    (see learn/transport.py). reauth=False (the always-on bot): never open a browser unasked.
    allow_login=False (launchd service): don't open one at startup either; an expired saved
    session is used as-is and the bot tells you to sign in. No browser stays open."""
    print("\nChecking LEARN session...")
    try:
        stored = learn_session.load()
    except ValueError:
        stored = None
    if stored is not None and auth.verify(stored) is not None:
        verified = stored
        print("LEARN session ✓ (saved)\n")
    elif not allow_login:
        if stored is None:
            raise AgentStartupError("No saved LEARN session. Run: python agent.py auth")
        verified = stored
        print("Saved LEARN session has expired -- I'll ask you to sign in once I'm running.\n")
    else:
        print("Opening a dedicated browser window. Complete the Waterloo login and Duo")
        print("authentication there (this app never sees your password).")
        try:
            verified = auth.authenticate()
        except LoginError as exc:
            raise AgentStartupError(str(exc)) from exc
        print("LEARN authentication ✓\n")
    return LearnClient(SessionTransport(verified, reauth=auth.authenticate if reauth else None))


def make_session_refresher(client: LearnClient):
    """Callable for the always-on bot: pick up a session that `agent.py auth` saved from another
    process. True if a verified session was loaded into the client's transport."""

    def refresh() -> bool:
        try:
            stored = learn_session.load()
            if stored is None or auth.verify(stored) is None:
                return False
            client.transport.replace_session(stored)
            return True
        except Exception:  # noqa: BLE001 -- "not yet" is the only useful answer to any failure
            return False

    return refresh


def connect_and_authenticate(
    backend: backend_module.Backend,
    *,
    reauth: bool = True,
    allow_login: bool = True,
    reminder_store=None,
) -> tuple[LearnClient, dict]:
    """Obtain a verified LEARN client and build the tool set. Shared by
    run_interactive_agent() (the CLI) and discord_bot.py; nothing for the
    caller to clean up."""
    client = obtain_client(reauth=reauth, allow_login=allow_login)
    decider = triage.default_decider()
    if decider is not None:
        print("Announcement triage ✓ (local)\n")
    tools = build_tools(
        client, decider=decider, snapshot_store=changes.SnapshotStore(), reminder_store=reminder_store
    )
    return client, tools


def run_interactive_agent(backend: backend_module.Backend, debug: bool = False) -> None:
    """Full interactive agent: stored/fresh LEARN session -> chat REPL."""
    print("LEARN Agent")
    print("───────────")

    llm_client = _connect(backend)

    try:
        _client, tools = connect_and_authenticate(backend)
    except AgentStartupError as exc:
        print(f"\n{exc}")
        return

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


def run_logout() -> None:
    """Forget the saved session. The dedicated browser profile is kept, so Duo
    stays trusted; use `auth --clear-profile` to reset that too."""
    try:
        existed = learn_session.load() is not None
    except ValueError:
        existed = True  # unreadable store still gets removed below
    learn_session.clear()
    if existed:
        print("Local session cleared. You will need to log in again next run.")
    else:
        print("No local session found. Nothing to clear.")


def run_auth(args: list[str]) -> int:
    """`python agent.py auth [--check | --paste | --clear-profile]`. Returns the exit code."""
    if "--clear-profile" in args:
        try:
            auth.clear_profile()
        except LoginError as exc:
            print(exc)
            return 1
        print("Saved session and dedicated browser profile removed.")
        return 0

    if "--check" in args:
        try:
            stored = learn_session.load()
        except ValueError:
            stored = None
        if stored is None or auth.verify(stored) is None:
            print("session expired or missing · run: python agent.py auth")
            return auth.EXIT_SESSION_EXPIRED
        print(f"LEARN session ✓ (saved {int(stored.age().total_seconds() // 60)} min ago)")
        return 0

    if "--stdin" in args:
        try:
            auth.authenticate_from_blob(transfer.read_blob(sys.stdin))
        except LoginError as exc:
            print(exc)
            return 1
        print("LEARN authentication ✓ — session saved.")
        return 0

    if "--export" in args or "--push" in args:
        return _run_export_or_push(args)

    backend = "paste" if "--paste" in args else "auto"
    try:
        auth.authenticate(backend=backend)
    except LoginError as exc:
        print(exc)
        return 1
    print("LEARN authentication ✓ — session saved.")
    return 0


def _run_export_or_push(args: list[str]) -> int:
    """`auth --export [--stdout]` / `auth --push user@host [--dir D] [--python P]`: hand the saved,
    verified session to a machine with no browser. See learn/transfer.py."""
    try:
        stored = learn_session.load()
    except ValueError:
        stored = None
    if stored is None or auth.verify(stored) is None:
        print("session expired or missing · run: python agent.py auth")
        return auth.EXIT_SESSION_EXPIRED
    try:
        if "--push" in args:
            host = _flag_value(args, "--push")
            if host is None:
                print("usage: python agent.py auth --push user@host [--dir ~/plnr] [--python .venv/bin/python]")
                return 1
            code = transfer.push(
                stored,
                host,
                directory=_flag_value(args, "--dir") or transfer.DEFAULT_REMOTE_DIR,
                python=_flag_value(args, "--python") or transfer.DEFAULT_REMOTE_PYTHON,
            )
            if code != 0:
                print(f"ssh/remote command failed (exit {code}); the session was not installed on {host}.")
            return code
        header = transfer.export_header(stored)
    except LoginError as exc:
        print(exc)
        return 1
    if "--stdout" in args:
        print(header)  # explicit request: this is a working login, don't leave it in scrollback
        return 0
    if transfer.copy_to_clipboard(header):
        print("Session cookies copied to the clipboard. Paste them on the server with: python agent.py auth --paste")
        return 0
    print("No clipboard available; re-run with --stdout to print them (treat the output like a password).")
    return 1


def _flag_value(args: list[str], flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def run_today(args: list[str]) -> int:
    days_raw = _flag_value(args, "--days")
    try:
        days = int(days_raw) if days_raw is not None else today_view.DEFAULT_UPCOMING_DAYS
    except ValueError:
        print("--days must be a whole number")
        return 2
    if not 1 <= days <= 365:
        print("--days must be between 1 and 365")
        return 2
    ics_path = _flag_value(args, "--ics")
    if "--ics" in args and ics_path is None:
        print("--ics needs a file path")
        return 2

    try:
        client = obtain_client()
        courses = client.get_courses()
    except (AgentStartupError, LearnApiError) as exc:
        print(f"\n{exc}")
        return 1
    now = datetime.now(timezone.utc)
    deadlines, errors, _active = today_view.collect_deadlines(client, courses)
    print(today_view.render_today(today_view.build_today(deadlines, now, upcoming_days=days), errors, now))
    if ics_path:
        Path(ics_path).write_text(today_view.to_ics(deadlines, now), encoding="utf-8", newline="")
        print(f"\nWrote {len(deadlines)} deadline(s) to {ics_path}")
    return 1 if errors else 0


def run_diff(args: list[str]) -> int:
    try:
        client = obtain_client()
        courses = client.get_courses()
    except (AgentStartupError, LearnApiError) as exc:
        print(f"\n{exc}")
        return 1
    result = changes.check_for_changes(client, courses, changes.SnapshotStore(), save="--no-save" not in args)
    print(changes.render(result))
    return 1 if result["errors"] else 0


def run_doctor() -> int:
    from agent import gemini_client
    from learn import cdp

    gemini_client.get_api_key()  # side effect: loads .env into os.environ so checks see it
    checks = doctor.run_checks(
        doctor.Deps(
            locate_browser=cdp.locate_browser,
            load_session=learn_session.load,
            verify_session=auth.verify,
            session_file=learn_session.SESSION_FILE,
            triage_available=lambda: triage.default_decider() is not None,
        )
    )
    print("LEARN Agent doctor\n")
    print(doctor.render(checks))
    return doctor.exit_code(checks)


def run_install_service(args: list[str]) -> int:
    import subprocess

    try:
        path = service.install(_flag_value(args, "--model"))
    except RuntimeError as exc:
        print(exc)
        return 1
    print(f"Wrote {path}")
    command = service.load_command(path)
    if "--load" in args:
        result = subprocess.run(command)
        print("Started." if result.returncode == 0 else f"launchctl exited {result.returncode} (already loaded?)")
        return result.returncode
    print("Start it with:\n  " + " ".join(command))
    print("Logs: .plnr/logs/bot.out.log and bot.err.log")
    return 0


def run_uninstall_service() -> int:
    import subprocess

    subprocess.run(service.unload_command())  # fine if it was never loaded
    path = service.plist_path()
    if path.exists():
        path.unlink()
        print(f"Removed {path}")
    else:
        print("No LaunchAgent was installed.")
    return 0


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "auth":
        sys.exit(run_auth(args[1:]))
    if args and args[0] == "today":
        sys.exit(run_today(args[1:]))
    if args and args[0] == "diff":
        sys.exit(run_diff(args[1:]))
    if args and args[0] == "doctor":
        sys.exit(run_doctor())
    if args and args[0] == "install-service":
        sys.exit(run_install_service(args[1:]))
    if args and args[0] == "uninstall-service":
        sys.exit(run_uninstall_service())
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
