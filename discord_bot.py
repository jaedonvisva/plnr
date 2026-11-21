"""Discord DM entry point for the LEARN agent.

    python discord_bot.py                    Connect the LLM backend, load the
                                              saved LEARN session (or open the
                                              same dedicated-browser WatIAM/Duo
                                              sign-in as agent.py), then start
                                              listening for Discord DMs.
    python discord_bot.py --model qwen       Use local Ollama/Qwen instead
                                              of the default Gemini backend
                                              (same --model flag as agent.py).
    python discord_bot.py --debug            Print tool-call debug info to
                                              this terminal only -- never to
                                              Discord.
    python discord_bot.py --service          Unattended mode (used by the launchd
                                              LaunchAgent, see `agent.py
                                              install-service`): never opens a
                                              browser; exits 75 if there is no
                                              saved LEARN session.

While running, the bot also watches LEARN and DMs you on its own: a morning digest,
"due tonight" and "last call" reminders, and changes (new announcements, grades, moved
dates). Reply "done 2", "mute 2h" or "stop tonight" to steer it. See agent/proactive.py.

This file is a TRANSPORT, not a second implementation of LEARN auth or the
agent loop -- it reuses agent.py's _connect()/connect_and_authenticate()
unmodified and hands everything else to discord_adapter/. See
docs/superpowers/specs/2026-08-19-discord-adapter-design.md for the design.

Fails closed: if the LLM backend or LEARN authentication doesn't succeed,
this exits before the Discord client ever connects -- the bot is never
visibly online while unable to actually answer anything.
"""

import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent import backend as backend_module
from discord_adapter.bot import LearnDiscordClient
from agent.proactive import ProactiveAgent
from agent.reminders import ReminderStore
from discord_adapter.config import DiscordConfigError, load_config
from discord_adapter.notifier import DiscordNotifier
from discord_adapter.session import AgentSession

# `import agent` is ambiguous in this directory: `agent.py` (this project's
# CLI script) and `agent/` (the package `discord_adapter.session` etc. pull
# from) share the same name. Python's import system prefers the package
# over the same-named module unconditionally, so a plain `import agent`
# silently binds to the PACKAGE, not the script -- `agent_module._connect`,
# `._model_arg`, `.connect_and_authenticate`, and `.AgentStartupError`
# (all defined in agent.py itself, not the package) would then be missing
# at runtime with an AttributeError, even though `import discord_bot`
# alone succeeds (nothing above touches those attributes at import time).
# Loading agent.py by its literal file path sidesteps the name collision
# without renaming either agent.py or the agent/ package.
_agent_py_path = Path(__file__).resolve().parent / "agent.py"
_agent_py_spec = importlib.util.spec_from_file_location("agent_cli", _agent_py_path)
agent_module = importlib.util.module_from_spec(_agent_py_spec)
_agent_py_spec.loader.exec_module(agent_module)


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

    service_mode = "--service" in args
    reminder_store = ReminderStore()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        # The bot never opens a browser mid-run (it may be 3 AM); an expired session is
        # reported over Discord instead. --service also forbids it at startup.
        learn_client, tools = agent_module.connect_and_authenticate(
            backend, reauth=False, allow_login=not service_mode, reminder_store=reminder_store
        )
    except agent_module.AgentStartupError as exc:
        print(f"\n{exc}")
        executor.shutdown(wait=False)
        sys.exit(75 if service_mode else 1)

    session = AgentSession(
        llm_client=llm_client,
        backend=backend,
        tools=tools,
        executor=executor,
        debug="--debug" in args,
        time_hint=True,
        finalize_reply=reminder_store.ensure_readbacks,
    )

    notifier = DiscordNotifier(discord_config.allowed_user_id)
    proactive = ProactiveAgent(
        learn_client,
        notifier,
        executor,
        reminders=reminder_store,
        refresh_session=agent_module.make_session_refresher(learn_client),
        on_sent=session.add_note,
    )

    print("Discord bot connecting...")
    client = LearnDiscordClient(session=session, config=discord_config, proactive=proactive)
    notifier.bind(client)
    try:
        client.run(discord_config.bot_token)
    finally:
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
