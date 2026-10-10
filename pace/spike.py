"""M0 spike: take one item from plan to merge in the terminal.

    uv run python -m pace.spike --origin <url> --slug toy --item-id TOY-003 \
        --title "..." --description "..."

Replaced by the real state machine in M1.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pace import git as g
from pace.agent import Agent, Mode, RunResult
from pace.prompts import render


class SpikeError(RuntimeError):
    pass


class TerminalUI:
    def show(self, text: str = "") -> None:
        print(text, flush=True)

    def event(self, e: dict) -> None:
        if e.get("type") == "assistant":
            for c in e["message"]["content"]:
                if c.get("type") == "tool_use":
                    detail = c["input"].get("description") or c["input"].get("file_path") or ""
                    print(f"  · {c['name']} {detail}"[:120], flush=True)

    def answer(self, q: dict) -> str:
        print(f"\n? {q['text']}\n  ({q['why_it_matters']})")
        for o in q["options"]:
            star = " (recommended)" if o["id"] == q.get("recommended") else ""
            print(f"  [{o['id']}] {o['label']}{star}: {o['description']}")
        reply = input("  answer (option id or free text, empty = recommended): ").strip()
        return reply or q.get("recommended", "")

    def review_plan(self, out: dict) -> tuple[str, str]:
        print("\n" + out["plan_markdown"])
        reply = input("\n[a]pprove / [f]eedback / [p]ark: ").strip().lower()
        if reply.startswith("f"):
            return "feedback", input("feedback: ")
        return ("approve", "") if reply.startswith("a") else ("park", "")

    def confirm(self, text: str) -> bool:
        return input(f"{text} [y/N]: ").strip().lower() == "y"


class Spike:
    def __init__(self, agent: Agent, project: g.Project, item_id: str, title: str,
                 description: str, ui, log_dir: Path, model: str = "", extra: str = ""):
        self.agent, self.p, self.ui = agent, project, ui
        self.item_id, self.title, self.description = item_id, title, description
        self.log_dir, self.model, self.extra = log_dir, model, extra
        self.session: str | None = None
        self.runs = 0

    # --- runs ---

    def _run(self, prompt: str, schema: str, mode: Mode) -> dict:
        wt = self.p.worktree(self.item_id)
        result = self._invoke(prompt, schema, mode)
        if not result.ok:
            # one retry that says what was wrong (§8.1)
            why = result.stderr.strip()[-500:] or "no valid structured output in the result"
            self.ui.show(f"! run failed ({why}); retrying once")
            result = self._invoke(f"Your previous turn failed: {why}\n"
                                  "Report through the structured output.", schema, mode)
            if not result.ok:
                raise SpikeError(f"run failed twice: {result.stderr.strip()[-500:]}")
        if mode == "read_only" and not g.is_clean(self.agent, wt):
            self.ui.show("! read-only run left changes in the worktree; resetting")
            g.reset_worktree(self.agent, wt)
        return result.structured_output  # type: ignore[return-value]

    def _invoke(self, prompt: str, schema: str, mode: Mode) -> RunResult:
        self.runs += 1
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_dir / f"{self.item_id}-{self.runs}.jsonl"
        self.ui.show(f"\n--- {schema} run #{self.runs} ({mode}), log: {log}")
        r = self.agent.claude_run(prompt, cwd=self.p.worktree(self.item_id), schema=schema, mode=mode,
                                  resume=self.session, model=self.model, log_path=log,
                                  on_event=self.ui.event)
        self.session = r.session_id or self.session
        if r.rate_limit and r.rate_limit.get("status") != "allowed":
            self.ui.show(f"! rate limit: {json.dumps(r.rate_limit)}")
        return r

    # --- flow ---

    def run(self) -> str:
        """Returns the outcome: merged | conflicts | ready_to_test | parked | blocked."""
        g.ensure_canonical(self.p)
        plan_base = g.add_worktree(self.agent, self.p, self.item_id)
        out = self._run(render("plan", item_id=self.item_id, title=self.title,
                               description=self.description, extra_instructions=self.extra),
                        "plan", "read_only")
        while True:
            self.ui.show(f"\n[{out['status']}] {out['summary']}")
            if out["status"] == "blocked":
                self.ui.show(f"blocked: {out['blocked_reason']}")
                return "blocked"
            if out["status"] == "needs_input":
                answers = [f"- {q['id']}: {self.ui.answer(q)}" for q in out["questions"]]
                out = self._run(render("answers", answers="\n".join(answers)), "plan", "read_only")
                continue
            decision, feedback = self.ui.review_plan(out)
            if decision == "approve":
                break
            if decision == "park":
                return "parked"
            out = self._run(render("revise_plan", feedback=feedback), "plan", "read_only")

        branch = g.branch_name(self.item_id)
        impl_base = g.reset_to_base(self.agent, self.p, self.item_id)
        out = self._run(render("implement", item_id=self.item_id, branch=branch,
                               plan_base=plan_base[:12], impl_base=impl_base[:12],
                               extra_instructions=self.extra), "implement", "write")
        self.ui.show(f"\n[{out['status']}] {out['summary']}")
        if out["status"] == "blocked":
            self.ui.show(f"blocked: {out['blocked_reason']}")
            return "blocked"
        wt = self.p.worktree(self.item_id)
        if g.head(self.agent, wt) == impl_base:
            raise SpikeError("implementation reported done but made no commits")
        if not g.is_clean(self.agent, wt):
            self.ui.show("! worktree not clean after implementation (uncommitted changes ignored)")

        self.ui.show("\nVerified by Claude:")
        for v in out["verified"]:
            self.ui.show(f"  ✓ {v}")
        self.ui.show("\nManual checks:")
        for m in out["manual_checks"]:
            self.ui.show(f"  [ ] {m}")
        if out["proposed_items"]:
            self.ui.show("\nProposed items:")
            for pi in out["proposed_items"]:
                self.ui.show(f"  + {pi['title']}: {pi['reason']}")

        g.transfer_branch(self.agent, self.p, self.item_id)
        g.push_branch(self.p, self.item_id)
        self.ui.show(f"\nPushed {branch} to {self.p.origin}")
        self.ui.show(f"  test with: git fetch origin && git checkout {branch}")
        if not self.ui.confirm("Works → merge?"):
            return "ready_to_test"
        m = g.merge_local(self.p, self.item_id, out["commit_message"])
        if not m.merged:
            self.ui.show(f"Conflicts (resolve_conflicts run is M1+):\n{m.conflicts}")
            return "conflicts"
        g.delete_remote_branch(self.p, self.item_id)
        g.remove_worktree(self.agent, self.p, self.item_id)
        self.ui.show(f"Merged as {m.commit[:12]} into {self.p.base}")
        return "merged"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origin", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--item-id", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--description-file", type=Path)
    ap.add_argument("--data-dir", type=Path, default=Path("/var/lib/pace"))
    ap.add_argument("--agent-user", default="pace-agent", help="empty: run as yourself")
    ap.add_argument("--agent-home", type=Path, default=Path("/home/pace-agent"))
    ap.add_argument("--claude", default="/home/pace-agent/.local/bin/claude")
    ap.add_argument("--model", default="")
    a = ap.parse_args(argv)

    desc = a.description_file.read_text() if a.description_file else a.description
    agent = Agent(user=a.agent_user or None, claude=a.claude)
    project = g.Project(a.slug, a.origin, a.base, a.data_dir, a.agent_home)
    spike = Spike(agent, project, a.item_id, a.title, desc, TerminalUI(),
                  a.data_dir / "logs", model=a.model)
    try:
        outcome = spike.run()
    except (SpikeError, g.GitError) as e:
        print(f"\nfailed: {e}", file=sys.stderr)
        return 1
    print(f"\noutcome: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
