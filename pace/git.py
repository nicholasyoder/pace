"""Git plumbing: canonical repos (PACE-owned) and agent clones/worktrees.

PACE never runs git inside a directory the agent can write to. Branches come
back from the agent as a bundle written to stdout by the agent's own git; PACE
saves it to a file it owns and fetches from that.

Layout:
  <data>/repos/<slug>.git                 canonical bare clone; refs/heads/* mirrors origin
  <data>/bundles/<ID>.bundle              transfer files
  <agent_home>/work/<slug>/repo.git       agent bare clone, remote `canon`
  <agent_home>/work/<slug>/wt/<ID>        worktree on branch pace/<ID>
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from pace.agent import Agent


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path | str | None = None, check: bool = True) -> str:
    argv = ["git"] + (["-C", str(cwd)] if cwd else []) + list(args)
    proc = subprocess.run(argv, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def branch_name(item_id: str) -> str:
    return f"pace/{item_id}"


@dataclass
class Project:
    slug: str
    origin: str
    base: str
    data_dir: Path
    agent_home: Path

    @property
    def canonical(self) -> Path:
        return self.data_dir / "repos" / f"{self.slug}.git"

    @property
    def agent_repo(self) -> str:
        return str(self.agent_home / "work" / self.slug / "repo.git")

    def worktree(self, item_id: str) -> str:
        return str(self.agent_home / "work" / self.slug / "wt" / item_id)


# --- canonical (as PACE) ---------------------------------------------------

def ensure_canonical(p: Project) -> None:
    if not p.canonical.exists():
        p.canonical.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", "--bare", p.origin, str(p.canonical))
        git("config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*", cwd=p.canonical)
    fetch_origin(p)


def fetch_origin(p: Project) -> str:
    """Mirror origin's branches into the canonical repo; return the base commit."""
    git("fetch", "--quiet", "--prune", "origin", cwd=p.canonical)
    return git("rev-parse", p.base, cwd=p.canonical)


def push_branch(p: Project, item_id: str) -> None:
    b = branch_name(item_id)
    git("push", "--quiet", "--force", "origin", f"{b}:refs/heads/{b}", cwd=p.canonical)


def delete_remote_branch(p: Project, item_id: str) -> None:
    git("push", "--quiet", "origin", "--delete", branch_name(item_id), cwd=p.canonical, check=False)


def is_ancestor(p: Project, a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(p.canonical), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def branch_diff(p: Project, item_id: str, stat: bool = False) -> str:
    """What merging pace/<ID> would change: diff from the merge base with <base>."""
    args = ["--stat"] if stat else []
    return git("diff", *args, f"{p.base}...{branch_name(item_id)}", cwd=p.canonical, check=False)


def branch_log(p: Project, item_id: str) -> str:
    return git("log", "--format=%h %s", f"{p.base}..{branch_name(item_id)}", cwd=p.canonical,
               check=False)


@dataclass
class MergeResult:
    merged: bool
    commit: str | None = None
    conflicts: str = ""


def merge_local(p: Project, item_id: str, message: str, strategy: str = "squash",
                attempts: int = 3) -> MergeResult:
    """merge-tree + commit-tree, then push the base. Retries on a non-ff push."""
    b = branch_name(item_id)
    for _ in range(attempts):
        base = fetch_origin(p)
        proc = subprocess.run(["git", "-C", str(p.canonical), "merge-tree", "--write-tree",
                               "--name-only", base, b], capture_output=True, text=True)
        if proc.returncode == 1:
            # first line is the tree; then conflicted files and messages
            return MergeResult(False, conflicts=proc.stdout.split("\n", 1)[-1].strip())
        if proc.returncode != 0:
            raise GitError(f"merge-tree: {proc.stderr.strip()}")
        tree = proc.stdout.split("\n", 1)[0].strip()
        parents = ["-p", base] + (["-p", git("rev-parse", b, cwd=p.canonical)]
                                  if strategy == "merge" else [])
        commit = git("commit-tree", tree, *parents, "-m", message, cwd=p.canonical)
        push = subprocess.run(["git", "-C", str(p.canonical), "push", "--quiet", "origin",
                               f"{commit}:refs/heads/{p.base}"], capture_output=True, text=True)
        if push.returncode == 0:
            git("update-ref", f"refs/heads/{p.base}", commit, cwd=p.canonical)
            return MergeResult(True, commit=commit)
        if "non-fast-forward" not in push.stderr and "rejected" not in push.stderr:
            raise GitError(f"push: {push.stderr.strip()}")
    raise GitError(f"push of {p.base} kept being rejected")


# --- agent side (as the agent user) ----------------------------------------

def ensure_agent_clone(agent: Agent, p: Project) -> None:
    probe = agent.git("rev-parse", "--git-dir", cwd=p.agent_repo, check=False)
    if probe.returncode != 0:
        agent.git("clone", "--quiet", "--bare", str(p.canonical), p.agent_repo)
        agent.git("remote", "rename", "origin", "canon", cwd=p.agent_repo)
        agent.git("config", "remote.canon.fetch", "+refs/heads/*:refs/remotes/canon/*",
                  cwd=p.agent_repo)
    agent.git("fetch", "--quiet", "--prune", "canon", cwd=p.agent_repo)


def add_worktree(agent: Agent, p: Project, item_id: str) -> str:
    """Put the item worktree on a fresh branch from canon/<base>; return its base commit.

    If the worktree already exists (a retried or unparked item) it is reset
    instead, keeping the path stable for session resume.
    """
    ensure_agent_clone(agent, p)
    wt = p.worktree(item_id)
    if agent.git("rev-parse", "--git-dir", cwd=wt, check=False).returncode == 0:
        reset_worktree(agent, wt)
        agent.git("checkout", "--quiet", "-B", branch_name(item_id), f"canon/{p.base}", cwd=wt)
    else:
        agent.git("worktree", "prune", cwd=p.agent_repo)
        agent.git("worktree", "add", "--quiet", "-B", branch_name(item_id), wt,
                  f"canon/{p.base}", cwd=p.agent_repo)
    return head(agent, wt)


def reset_to_base(agent: Agent, p: Project, item_id: str) -> str:
    """Implement start: move the worktree's branch onto the latest base."""
    agent.git("fetch", "--quiet", "--prune", "canon", cwd=p.agent_repo)
    wt = p.worktree(item_id)
    reset_worktree(agent, wt)
    agent.git("checkout", "--quiet", "-B", branch_name(item_id), f"canon/{p.base}", cwd=wt)
    return head(agent, wt)


def head(agent: Agent, wt: str) -> str:
    return agent.git("rev-parse", "HEAD", cwd=wt).stdout.decode().strip()


def is_clean(agent: Agent, wt: str) -> bool:
    out = agent.git("status", "--porcelain", "--untracked-files=all", cwd=wt).stdout
    return not out.strip()


def reset_worktree(agent: Agent, wt: str) -> None:
    agent.git("reset", "--quiet", "--hard", cwd=wt)
    agent.git("clean", "-fdq", cwd=wt)


def transfer_branch(agent: Agent, p: Project, item_id: str) -> str:
    """Bundle pace/<ID> on the agent side and fetch it into the canonical repo."""
    b = branch_name(item_id)
    bundles = p.data_dir / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    path = bundles / f"{item_id}.bundle"
    with open(path, "wb") as f:
        agent.git("bundle", "create", "-", b, f"^canon/{p.base}", cwd=p.worktree(item_id),
                  stdout=f)
    git("bundle", "verify", "--quiet", str(path), cwd=p.canonical)
    git("fetch", "--quiet", str(path), f"+refs/heads/{b}:refs/heads/{b}", cwd=p.canonical)
    path.unlink()
    return git("rev-parse", b, cwd=p.canonical)


def remove_worktree(agent: Agent, p: Project, item_id: str) -> None:
    agent.git("worktree", "remove", "--force", p.worktree(item_id), cwd=p.agent_repo, check=False)
    agent.git("branch", "-D", branch_name(item_id), cwd=p.agent_repo, check=False)
