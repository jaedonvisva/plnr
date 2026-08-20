import ast
from pathlib import Path

BOT_PY = Path(__file__).resolve().parent.parent.parent / "discord_adapter" / "bot.py"


def _imported_module_names() -> set[str]:
    tree = ast.parse(BOT_PY.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_bot_module_never_imports_learn_or_agent_tools():
    names = _imported_module_names()
    for name in names:
        assert not name.startswith("learn"), f"discord_adapter/bot.py must not import {name}"
        assert name not in ("agent.tools", "agent.course_registry"), (
            f"discord_adapter/bot.py must not import {name}"
        )


def test_agent_session_is_never_constructed_inside_bot_module():
    source = BOT_PY.read_text()
    assert "AgentSession(" not in source
