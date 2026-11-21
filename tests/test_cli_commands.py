"""Wiring tests for `agent.py today|diff|doctor` with a fake client (no network/LLM/browser)."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import changes
from learn.models import Assignment, Course

_spec = importlib.util.spec_from_file_location("agent_cli", Path(__file__).resolve().parent.parent / "agent.py")
agent_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_cli)


class FakeClient:
    def __init__(self, due):
        self.due = due
        self.course = Course(id="1", name="Intro", code=None, is_active=True, url="u", last_accessed_at=None, display_code="MSE 232")

    def get_courses(self): return [self.course]
    def get_assignments(self, cid):
        return [Assignment(id="a", course_id="1", name="HW 1", description=None, due_at=self.due, max_points=10,
                           grade_item_id=None, points_earned=None, points_possible=None, is_hidden=False)]
    def get_quizzes(self, cid): return []
    def get_announcements(self, cid): return []
    def get_grades(self, cid): return []


def test_today_prints_buckets_and_writes_ics(monkeypatch, tmp_path, capsys):
    due = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(agent_cli, "obtain_client", lambda: FakeClient(due))
    ics = tmp_path / "out.ics"
    code = agent_cli.run_today(["--days", "7", "--ics", str(ics)])
    out = capsys.readouterr().out
    assert code == 0 and "[MSE 232] HW 1" in out and f"Wrote 1 deadline(s) to {ics}" in out
    data = ics.read_bytes()
    assert data.startswith(b"BEGIN:VCALENDAR\r\n") and b"SUMMARY:MSE 232: HW 1 (due)" in data


@pytest.mark.parametrize("args", [["--days", "x"], ["--days", "0"], ["--days", "999"], ["--ics"]])
def test_today_rejects_bad_arguments_before_touching_the_network(monkeypatch, args, capsys):
    monkeypatch.setattr(agent_cli, "obtain_client", lambda: pytest.fail("must not sign in"))
    assert agent_cli.run_today(args) == 2


def test_today_reports_startup_failure(monkeypatch, capsys):
    def boom():
        raise agent_cli.AgentStartupError("sign-in failed")

    monkeypatch.setattr(agent_cli, "obtain_client", boom)
    assert agent_cli.run_today([]) == 1 and "sign-in failed" in capsys.readouterr().out


def test_diff_baseline_then_change_and_no_save(monkeypatch, tmp_path, capsys):
    store_path = tmp_path / ".plnr" / "snapshot.json"
    real_store = changes.SnapshotStore
    monkeypatch.setattr(agent_cli.changes, "SnapshotStore", lambda: real_store(store_path))
    due = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(agent_cli, "obtain_client", lambda: FakeClient(due))

    assert agent_cli.run_diff([]) == 0
    assert "baseline" in capsys.readouterr().out

    monkeypatch.setattr(agent_cli, "obtain_client", lambda: FakeClient(due + timedelta(days=3)))
    assert agent_cli.run_diff(["--no-save"]) == 0
    assert "due date" in capsys.readouterr().out
    assert agent_cli.run_diff([]) == 0  # --no-save didn't advance the baseline, so it's reported again
    assert "due date" in capsys.readouterr().out
    assert agent_cli.run_diff([]) == 0
    assert "nothing changed" in capsys.readouterr().out


def test_doctor_runs_and_never_prints_secrets(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-VERY-SECRET")
    code = agent_cli.run_doctor()
    out = capsys.readouterr().out
    assert "VERY-SECRET" not in out and "Next step" in out or "Everything looks good" in out
    assert code in (0, 1)
