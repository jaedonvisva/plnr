"""Discord DM entry point for the LEARN agent.

    python discord_bot.py                    Connect the LLM backend, open
                                              LEARN auth (same visible
                                              Chromium + WatIAM/Duo flow as
                                              agent.py), wait for you to
                                              finish logging in, then start
                                              listening for Discord DMs.
    python discord_bot.py --model qwen       Use local Ollama/Qwen instead
                                              of the default Gemini backend
                                              (same --model flag as agent.py).
    python discord_bot.py --debug            Print tool-call debug info to
                                              this terminal only -- never to
                                              Discord.

This file is a TRANSPORT, not a second implementation of LEARN auth or the
agent loop -- it reuses agent.py's _connect()/connect_and_authenticate()
unmodified and hands everything else to discord_adapter/. See
docs/superpowers/specs/2026-08-19-discord-adapter-design.md for the design.

Fails closed: if the LLM backend or LEARN authentication doesn't succeed,
this exits before the Discord client ever connects -- the bot is never
visibly online while unable to actually answer anything.
"""

import sys
from concurrent.futures import ThreadPoolExecutor

import agent as agent_module
from agent import backend as backend_module
from discord_adapter.bot import LearnDiscordClient
from discord_adapter.config import DiscordConfigError, load_config
from discord_adapter.session import AgentSession


def _shutdown_browser(context, playwright, executor: ThreadPoolExecutor) -> None:
    """Close the Playwright context and stop Playwright itself, both on
    the executor's single worker thread -- the same thread that created
    them. Playwright's sync API is thread-affine (see AgentSession's
    docstring in discord_adapter/session.py); calling context.close() or
    playwright.stop() from any other thread raises
    `greenlet.error: Cannot switch to a different thread` on stop(), and
    silently no-ops close() under the bare `except Exception: pass` here.
    Blocks up to 30s for cleanup to finish before giving up."""

    def _cleanup():
        try:
            context.close()
        except Exception:
            pass
        playwright.stop()

    try:
        executor.submit(_cleanup).result(timeout=30)
    except Exception as exc:
        print(f"Browser cleanup failed: {exc}")


def main() -> None:
    args = sys.argv[1:]

    try:
        discord_config = load_config()
    except DiscordConfigError as exc:
        print(f"\n{exc}")
        sys.exit(1)

    backend = backend_module.resolve(agent_module._model_arg(args))

    print("LEARN Discord Agent")
    print("────────────────────")

    llm_client = agent_module._connect(backend)

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        context, playwright, _learn_client, tools = executor.submit(
            agent_module.connect_and_authenticate, backend
        ).result()
    except agent_module.AgentStartupError as exc:
        print(f"\n{exc}")
        executor.shutdown(wait=False)
        sys.exit(1)

    session = AgentSession(
        llm_client=llm_client,
        backend=backend,
        tools=tools,
        executor=executor,
        debug="--debug" in args,
    )

    print("Discord bot connecting...")
    client = LearnDiscordClient(session=session, config=discord_config)
    try:
        client.run(discord_config.bot_token)
    finally:
        _shutdown_browser(context, playwright, executor)
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
