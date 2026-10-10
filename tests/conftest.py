import subprocess
from pathlib import Path

import pytest

from pace import items as it
from pace.agent import Agent
from pace.config import Config
from pace.db import DB
from pace.runs import Runner

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


def push_to_origin(tmp_path: Path, origin: Path, name: str, content: str) -> None:
    """Someone else pushes a change to origin/main."""
    clone = tmp_path / f"other-{name}"
    if not clone.exists():
        sh("git", "clone", "-q", str(origin), str(clone))
    sh("git", "pull", "-q", cwd=clone)
    (clone / name).write_text(content)
    sh("git", *IDENT, "add", ".", cwd=clone)
    sh("git", *IDENT, "commit", "-qm", f"change {name}", cwd=clone)
    sh("git", "push", "-q", "origin", "main", cwd=clone)


class Scenario:
    """Queue fake_claude fixtures: each line is used by one claude invocation."""

    def __init__(self, path: Path):
        self.path = path
        path.write_text("")

    def __call__(self, *lines: str) -> None:
        with open(self.path, "a") as f:
            f.write("".join(line + "\n" for line in lines))

    def left(self) -> list[str]:
        return [x for x in self.path.read_text().splitlines() if x.strip()]


@pytest.fixture
def scenario(tmp_path, monkeypatch) -> Scenario:
    s = Scenario(tmp_path / "scenario")
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(s.path))
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", str(tmp_path / "calls.jsonl"))
    return s


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "data", agent_user="", agent_home=tmp_path / "agent",
                  claude=FAKE_CLAUDE)


@pytest.fixture
def db(cfg) -> DB:
    return DB(cfg.db_path)


@pytest.fixture
def runner(db, cfg) -> Runner:
    return Runner(db, cfg, Agent(user=None, claude=cfg.claude))


@pytest.fixture
def project(db, origin):
    return it.create_project(db, slug="TOY", name="Toy", origin=str(origin), base_branch="main")
