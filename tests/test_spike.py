"""End-to-end spike flow against fake_claude, as the current user (no sudo)."""

import subprocess
from pathlib import Path

import pytest

from pace import git as g
from pace.agent import Agent
from pace.spike import Spike

FAKE_CLAUDE = str(Path(__file__).parent / "fake_claude")
IDENT = ["-c", "user.name=t", "-c", "user.email=t@localhost"]


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=src)
    (src / "README.md").write_text("toy\n")
    sh("git", *IDENT, "add", ".", cwd=src)
    sh("git", *IDENT, "commit", "-qm", "init", cwd=src)
    bare = tmp_path / "origin.git"
    sh("git", "clone", "-q", "--bare", str(src), str(bare))
    return bare


class ScriptedUI:
    def __init__(self, decision="approve", merge=True, on_confirm=None):
        self.decision, self.merge, self.on_confirm = decision, merge, on_confirm
        self.lines: list[str] = []
        self.answered: list[str] = []

    def show(self, text=""):
        self.lines.append(text)

    def event(self, e):
        pass

    def answer(self, q):
        self.answered.append(q["id"])
        return q["recommended"]

    def review_plan(self, out):
        return self.decision, ""

    def confirm(self, text):
        if self.on_confirm:
            self.on_confirm()
        return self.merge


def make_spike(tmp_path: Path, origin: Path, ui) -> Spike:
    project = g.Project("toy", str(origin), "main", tmp_path / "data", tmp_path / "agent")
    agent = Agent(user=None, claude=FAKE_CLAUDE)
    return Spike(agent, project, "TOY-001", "Greet many", "Support greeting multiple people.",
                 ui, tmp_path / "data" / "logs")


def test_plan_answer_implement_merge(tmp_path, origin):
    ui = ScriptedUI()
    spike = make_spike(tmp_path, origin, ui)
    assert spike.run() == "merged"

    assert ui.answered == ["q1_format"]
    assert spike.runs == 3  # plan, answers, implement
    assert spike.session == "0a607ce7-5920-466c-95a2-73a43f80ec7c"
    log = sh("git", "log", "--format=%s", "main", cwd=origin).splitlines()
    assert log == ["TOY-002: greet multiple people", "init"]  # squash with Claude's message
    assert sh("git", "show", "main:fake_change.txt", cwd=origin) == "implemented by fake_claude"
    assert sh("git", "branch", "--list", "pace/*", cwd=origin) == ""  # remote branch deleted
    assert not Path(spike.p.worktree("TOY-001")).exists()
    assert any("Manual checks:" in line for line in ui.lines)


def test_conflict_stops_before_merge(tmp_path, origin):
    def push_conflicting_change():
        clone = tmp_path / "other"
        sh("git", "clone", "-q", str(origin), str(clone))
        (clone / "fake_change.txt").write_text("someone else's version\n")
        sh("git", *IDENT, "add", ".", cwd=clone)
        sh("git", *IDENT, "commit", "-qm", "conflicting", cwd=clone)
        sh("git", "push", "-q", "origin", "main", cwd=clone)

    # base moves while the branch is waiting in ready_to_test
    ui = ScriptedUI(on_confirm=push_conflicting_change)
    spike = make_spike(tmp_path, origin, ui)
    assert spike.run() == "conflicts"
    assert "fake_change.txt" in ui.lines[-1]
    assert sh("git", "log", "-1", "--format=%s", "main", cwd=origin) == "conflicting"
    assert sh("git", "branch", "--list", "pace/TOY-001", cwd=origin) != ""  # left for review


def test_park(tmp_path, origin):
    spike = make_spike(tmp_path, origin, ScriptedUI(decision="park"))
    assert spike.run() == "parked"
    assert spike.runs == 2
