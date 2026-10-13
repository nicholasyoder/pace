"""Run manager: Claude runs and merges for items, each in a worker thread (§8, §10).

The caller (an action) has already moved the item into `planning`,
`implementing` or `merging`; the runner does the git preparation, runs
Claude, and applies the result as the next transition. Any failure moves the
item to `failed` with the reason; Retry re-runs the same kind with the same
inputs. After a restart, `recover()` queues interrupted items for the scheduler
to retry the same way.

Every run or merge start counts as an attempt in the item's current state
(§9 rule 5); a start past MAX_ATTEMPTS is refused and fails the item. The
count resets when the item moves on (see items.transition) and on Retry.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from pace import git as g
from pace import items as it
from pace.agent import Agent, AgentError, RunResult, stop
from pace.bus import Bus
from pace.config import Config
from pace.db import DB, now
from pace.prompts import render
from pace.roadmap import RoadmapError, format_id

PLAN_KINDS = ("plan", "import", "answers", "revise_plan")
IMPL_KINDS = ("implement", "address_feedback", "resolve_conflicts")
MAX_ATTEMPTS = 3


class RunFailed(RuntimeError):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


class Busy(RuntimeError):
    pass


def git_project(cfg: Config, p: sqlite3.Row) -> g.Project:
    key = g.key_path(cfg.data_dir, p["slug"])
    use_key = key.exists() and g.github_repo(p["origin"]) is not None
    return g.Project(p["slug"].lower(), p["origin"], p["base_branch"], cfg.data_dir,
                     cfg.agent_home, ssh_key=key if use_key else None)


def activity_line(e: dict) -> str | None:
    """One human-readable line for a stream event, or None to skip it."""
    if e.get("type") == "assistant":
        parts = []
        for c in e["message"].get("content", []):
            if c.get("type") == "tool_use":
                inp = c.get("input") or {}
                detail = (inp.get("description") or inp.get("file_path") or inp.get("pattern")
                          or inp.get("command") or "")
                parts.append(f"· {c['name']} {detail}".rstrip())
            elif c.get("type") == "text" and c.get("text", "").strip():
                parts.append(c["text"].strip())
        return "\n".join(parts)[:500] or None
    if e.get("type") == "result":
        return f"· result: {e.get('subtype')} ({e.get('num_turns')} turns)"
    return None


def is_rate_limited(r: RunResult) -> bool:
    """Loose on purpose: the exact failure shape hasn't been seen yet (§8.1)."""
    if r.rate_limit and r.rate_limit.get("status") not in (None, "allowed"):
        return True
    text = (str((r.result_event or {}).get("result", "")) + r.stderr).lower()
    return not r.ok and any(s in text for s in ("rate limit", "usage limit", "rate_limit"))


def resume_failed(r: RunResult) -> bool:
    """The session couldn't be resumed (e.g. it was created on another VM).

    claude still emits a result event for this: subtype error_during_execution,
    zero turns, errors ["No conversation found with session ID: …"].
    """
    res = r.result_event
    if res is None:
        return True
    return bool(res.get("is_error")) and (res.get("num_turns") == 0 or
                                          "No conversation found" in str(res.get("errors")))


def latest_impl_output(db: DB, item_id: str) -> dict | None:
    row = db.one("SELECT output_json FROM runs WHERE item_id = ? AND status = 'succeeded' AND "
                 f"kind IN ({', '.join('?' * len(IMPL_KINDS))}) ORDER BY id DESC LIMIT 1",
                 item_id, *IMPL_KINDS)
    return json.loads(row["output_json"]) if row else None


def latest_plan(db: DB, item_id: str) -> sqlite3.Row | None:
    return db.one("SELECT * FROM plans WHERE item_id = ? ORDER BY version DESC LIMIT 1", item_id)


class Runner:
    def __init__(self, db: DB, cfg: Config, agent: Agent, bus: Bus | None = None):
        self.db, self.cfg, self.agent = db, cfg, agent
        self.bus = bus or Bus()
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._procs: dict[str, subprocess.Popen] = {}

    # --- public -------------------------------------------------------------

    def busy(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self._threads

    def active(self) -> int:
        with self._lock:
            return len(self._threads)

    def can_start(self) -> bool:
        return self.active() < self.cfg.concurrency

    def start(self, item_id: str, kind: str, inputs: dict | None = None) -> None:
        self._spawn(item_id, lambda: self._run(item_id, kind, inputs or {}))

    def start_merge(self, item_id: str) -> None:
        self._spawn(item_id, lambda: self._merge(item_id))

    def cancel(self, item_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(item_id)
        if proc is None:
            return False
        threading.Thread(target=stop, args=(proc,), daemon=True).start()
        return True

    def wait(self, timeout: float | None = None) -> None:
        """Join all worker threads, including ones started meanwhile (tests)."""
        while True:
            with self._lock:
                threads = list(self._threads.values())
            if not threads:
                return
            for t in threads:
                t.join(timeout)

    def recover(self) -> None:
        """Startup: runs left `running` are interrupted and their items queued to resume.

        An item whose interrupted run (or merge) can be redone exactly goes to
        `failed` flagged `interrupted`; the scheduler retries it first thing
        (§9). Otherwise (e.g. PACE stopped between the transition and the run
        starting) it just fails, and Retry is up to me.
        """
        cut: set[int] = set()
        for r in self.db.all("SELECT * FROM runs WHERE status IN ('queued', 'running')"):
            if r["pid"] and _is_sudo(r["pid"]):
                try:
                    os.kill(r["pid"], 15)
                except OSError:
                    pass
            self.db.run("UPDATE runs SET status = 'interrupted', ended_at = ?, "
                        "error = 'PACE restarted during the run' WHERE id = ?", now(), r["id"])
            cut.add(r["id"])
        for row in self.db.all("SELECT id, state FROM items WHERE state IN "
                               "('planning', 'implementing', 'merging')"):
            exact = row["state"] == "merging"
            if not exact:
                kinds = PLAN_KINDS if row["state"] == "planning" else IMPL_KINDS
                last = self.db.one("SELECT id, kind FROM runs WHERE item_id = ? "
                                   "ORDER BY id DESC LIMIT 1", row["id"])
                exact = last is not None and last["id"] in cut and last["kind"] in kinds
            it.transition(self.db, row["id"], "failed", "system", data={"interrupted": exact},
                          last_error="interrupted: PACE restarted during the run; it resumes "
                          "automatically" if exact else
                          "interrupted: PACE restarted while this was in progress")

    def publish(self, item_id: str, msg: dict | None = None) -> None:
        msg = msg or {"type": "state"}
        self.bus.publish(f"item:{item_id}", msg)
        if msg["type"] != "stream":
            self.bus.publish("global", {"type": "changed", "item": item_id})

    # --- threads ------------------------------------------------------------------

    def _spawn(self, item_id: str, fn: Callable[[], None]) -> None:
        with self._lock:
            if item_id in self._threads:
                raise Busy(f"{item_id} already has a run in progress")
            t = threading.Thread(target=self._guard, args=(item_id, fn), daemon=True,
                                 name=f"run-{item_id}")
            self._threads[item_id] = t
        t.start()

    def _guard(self, item_id: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # anything unexpected still lands the item in `failed`
            detail = f"{type(e).__name__}: {e}"
            if not isinstance(e, (RunFailed, g.GitError, AgentError)):
                detail += "\n" + traceback.format_exc(limit=5)
            try:
                if it.item(self.db, item_id)["state"] in it.BUSY:
                    it.transition(self.db, item_id, "failed", "system", last_error=detail)
            except Exception:
                traceback.print_exc()
        finally:
            with self._lock:
                self._threads.pop(item_id, None)
                self._procs.pop(item_id, None)
            self.publish(item_id)

    def _transition(self, item_id: str, to: str, actor: str = "system", **kw: Any) -> None:
        it.transition(self.db, item_id, to, actor, **kw)
        self.publish(item_id)

    def _attempt(self, item_id: str) -> None:
        """Count a start in the item's current state; refuse it past MAX_ATTEMPTS."""
        with self.db.tx():
            self.db.run("UPDATE items SET attempts = attempts + 1 WHERE id = ?", item_id)
            row = it.item(self.db, item_id)
        if row["attempts"] > MAX_ATTEMPTS:
            msg = f"gave up after {MAX_ATTEMPTS} attempts in {row['state']}"
            if row["last_error"]:
                msg += f"; last error: {row['last_error']}"
            raise RunFailed(msg)

    # --- a Claude run ------------------------------------------------------------

    def _run(self, item_id: str, kind: str, inputs: dict) -> None:
        self._attempt(item_id)
        cur = self.db.run("INSERT INTO runs (item_id, kind, input_json, status, started_at) "
                          "VALUES (?, ?, ?, 'queued', ?)", item_id, kind, json.dumps(inputs), now())
        run_id = cur.lastrowid
        log = self.cfg.log_dir / f"{run_id}.jsonl"
        self.db.run("UPDATE runs SET log_path = ? WHERE id = ?", str(log) + ".gz", run_id)
        self.publish(item_id)
        try:
            self._run_inner(run_id, item_id, kind, inputs, log)
        except Exception as e:
            status = e.status if isinstance(e, RunFailed) else "failed"
            self.db.run("UPDATE runs SET status = ?, ended_at = ?, error = ? "
                        "WHERE id = ? AND status IN ('queued', 'running')",
                        status, now(), str(e), run_id)
            raise
        finally:
            _gzip(log)

    def _run_inner(self, run_id: int, item_id: str, kind: str, inputs: dict, log: Path) -> None:
        item = it.item(self.db, item_id)
        proj = it.project(self.db, item["project_id"])
        P = git_project(self.cfg, proj)
        wt = P.worktree(item_id)
        session = item["session_id"]
        head_before = None

        if kind in ("plan", "import"):
            g.ensure_canonical(P)
            base = g.add_worktree(self.agent, P, item_id)
            session = None  # a plan run always starts a new conversation
            it.update(self.db, item_id, plan_base_commit=base, branch=g.branch_name(item_id),
                      session_id=None)
        elif g.ensure_worktree(self.agent, P, item_id):
            it.event(self.db, item_id, "worktree_recreated", "system",
                     {"message": "the item worktree was missing and has been recreated"})
        if kind == "implement":
            g.ensure_canonical(P)
            base = g.reset_to_base(self.agent, P, item_id)
            it.update(self.db, item_id, impl_base_commit=base)
            head_before = base
        elif kind in ("address_feedback", "resolve_conflicts"):
            if kind == "resolve_conflicts":
                g.ensure_canonical(P)
                g.ensure_agent_clone(self.agent, P)  # fresh canon/<base> to merge
            g.reset_worktree(self.agent, wt)
            head_before = g.head(self.agent, wt)

        item = it.item(self.db, item_id)
        prompt = self._prompt(kind, item, proj, inputs)
        out = self._claude(run_id, item, proj, kind, prompt, session, log)

        if kind in PLAN_KINDS:
            self._complete_plan(item, proj, out)
        else:
            self._complete_impl(item, proj, P, kind, out, head_before)

    def _claude(self, run_id: int, item: sqlite3.Row, proj: sqlite3.Row, kind: str, prompt: str,
                session: str | None, log: Path) -> dict:
        if kind in PLAN_KINDS:
            schema = "import" if item["type"] == "import" else "plan"
        else:
            schema = "implement"
        mode = "read_only" if kind in PLAN_KINDS else "write"
        item_id = item["id"]
        wt = git_project(self.cfg, proj).worktree(item_id)
        log.parent.mkdir(parents=True, exist_ok=True)

        def on_start(proc: subprocess.Popen) -> None:
            with self._lock:
                self._procs[item_id] = proc
            self.db.run("UPDATE runs SET status = 'running', pid = ? WHERE id = ?", proc.pid, run_id)

        def on_event(e: dict) -> None:
            line = activity_line(e)
            if line:
                self.publish(item_id, {"type": "stream", "run": run_id, "line": line})

        def invoke(p: str, resume: str | None) -> RunResult:
            r = self.agent.claude_run(p, cwd=wt, schema=schema, mode=mode, resume=resume,
                                      model=proj["model"], log_path=log, on_event=on_event,
                                      on_start=on_start)
            if r.rate_limit:
                self.db.kv_set("rate_limit", {**r.rate_limit, "seen_at": now()})
            return r

        # one retry (§8.1), for a failed run or for output that breaks the contract
        r = invoke(prompt, session)
        for attempt in (1, 2):
            if not r.ok:
                if is_rate_limited(r):
                    raise RunFailed(f"rate limited: {json.dumps(r.rate_limit)}", "rate_limited")
                if r.exit_code < 0 or r.exit_code == 143:
                    raise RunFailed(f"run was stopped (exit {r.exit_code})")
                if attempt == 2:
                    raise RunFailed("run failed twice: "
                                    f"{r.stderr.strip()[-500:] or 'no valid structured output'}")
                if session and resume_failed(r):
                    why = "resume failed; starting a fresh session"
                    retry_prompt, session = self._fresh(item, prompt), None
                else:
                    why = (r.stderr.strip()[-500:] or "no valid structured output in the result")
                    retry_prompt = (f"Your previous turn failed: {why}\n"
                                    "Report through the structured output.")
                    session = r.session_id or session
            else:
                problem = self._validate(item, r.structured_output)  # type: ignore[arg-type]
                if problem is None:
                    break
                if attempt == 2:
                    raise RunFailed(f"contract violation (after one retry): {problem}")
                why = problem
                retry_prompt = (f"PACE rejected your output: {problem}\n"
                                "Fix it and report again through the structured output.")
                session = r.session_id or session
            it.event(self.db, item_id, "run_retry", "system", {"run": run_id, "why": why})
            r = invoke(retry_prompt, session)

        if mode == "read_only" and not g.is_clean(self.agent, wt):
            g.reset_worktree(self.agent, wt)
            it.event(self.db, item_id, "readonly_violation", "system",
                     {"run": run_id, "message": "read-only run left changes; worktree reset"})

        res = r.result_event or {}
        total = res.get("total_cost_usd")
        cost = None
        if total is not None and r.session_id:
            prior = self.db.one("SELECT COALESCE(SUM(cost_usd), 0) FROM runs "
                                "WHERE session_id = ? AND id != ?", r.session_id, run_id)[0]
            cost = max(total - prior, 0)
        self.db.run("UPDATE runs SET status = 'succeeded', ended_at = ?, exit_code = ?, "
                    "session_id = ?, cost_usd = ?, usage_json = ?, output_json = ? WHERE id = ?",
                    now(), r.exit_code, r.session_id, cost, json.dumps(res.get("usage")),
                    json.dumps(r.structured_output), run_id)
        it.update(self.db, item_id, session_id=r.session_id)
        return r.structured_output  # type: ignore[return-value]

    def _validate(self, item: sqlite3.Row, out: dict) -> str | None:
        """Contract checks beyond the JSON schema; the message goes back to Claude."""
        status = out.get("status")
        if status == "needs_input" and not out.get("questions"):
            return "status is needs_input but there are no questions"
        if item["type"] == "import":
            md = (out.get("roadmap_markdown") or "").strip()
            if status == "plan_ready":
                if not md:
                    return "status is plan_ready but roadmap_markdown is empty"
                try:
                    it.check_import(self.db, item["project_id"], md)
                except RoadmapError as e:
                    return f"roadmap_markdown doesn't pass the PACE roadmap parser:\n{e}"
        elif status == "plan_ready" and not (out.get("plan_markdown") or "").strip():
            return "status is plan_ready but plan_markdown is empty"
        return None

    # --- prompts --------------------------------------------------------------------

    def _prompt(self, kind: str, item: sqlite3.Row, proj: sqlite3.Row, inputs: dict) -> str:
        extra = proj["extra_instructions"]
        branch = g.branch_name(item["id"])
        if kind == "plan":
            return render("plan", item_id=item["id"], title=item["title"],
                          description=item["description"] or "(no description; ask if unclear)",
                          context=self._context(item, proj),
                          dependencies=self._dependencies(item), extra_instructions=extra)
        if kind == "import":
            first, second = (format_id(proj["slug"], proj["next_id"] + i) for i in (0, 1))
            notes = item["description"].strip()
            current = it.active_markdown(self.db, proj["id"]).strip()
            existing = (f"\n## The project's current PACE roadmap\n\nDon't duplicate these items. "
                        f"New items may depend on them by ID.\n\n```markdown\n{current}\n```\n"
                        if current else "")
            return render("import", first_id=first, second_id=second, slug=proj["slug"],
                          instructions=f"## Notes from the reviewer\n\n{notes}\n\n" if notes else "",
                          existing=existing) + (f"\n{extra}\n" if extra else "")
        if kind == "answers":
            return render("answers", answers=inputs["answers"])
        if kind == "revise_plan":
            return render("revise_plan", feedback=inputs["feedback"])
        if kind == "implement":
            return render("implement", item_id=item["id"], branch=branch,
                          plan_base=(item["plan_base_commit"] or "")[:12],
                          impl_base=(item["impl_base_commit"] or "")[:12],
                          extra_instructions=extra)
        if kind == "address_feedback":
            return render("address_feedback", item_id=item["id"], branch=branch,
                          feedback=inputs["feedback"], extra_instructions=extra)
        if kind == "resolve_conflicts":
            return render("resolve_conflicts", item_id=item["id"], branch=branch,
                          base=proj["base_branch"], conflicts=inputs["conflicts"],
                          extra_instructions=extra)
        raise ValueError(f"unknown run kind {kind}")

    def _context(self, item: sqlite3.Row, proj: sqlite3.Row) -> str:
        """The roadmap preamble and the item's group description, if any."""
        parts = []
        if proj["preamble"].strip():
            parts.append(f"## Roadmap context\n\n{proj['preamble'].strip()}")
        if item["group_id"] is not None:
            grp = self.db.one("SELECT * FROM groups WHERE id = ?", item["group_id"])
            desc = f"\n\n{grp['description'].strip()}" if grp["description"].strip() else ""
            parts.append(f"## Part of: {grp['title']}{desc}")
        return ("\n" + "\n\n".join(parts) + "\n") if parts else ""

    def _dependencies(self, item: sqlite3.Row) -> str:
        parts = []
        for d in it.depends(item):
            dep = self.db.one("SELECT * FROM items WHERE id = ?", d)
            plan = latest_plan(self.db, d)
            if dep is None:
                continue
            body = plan["markdown"] if plan else "(no plan yet)"
            parts.append(f"### {d}: {dep['title']} ({dep['state']})\n\n{body}")
        if not parts:
            return ""
        return ("\n## Dependencies\n\nThis item builds on these items. Their approved plans "
                "(merged ones are already in the code):\n\n" + "\n\n".join(parts) + "\n")

    def _fresh(self, item: sqlite3.Row, prompt: str) -> str:
        """Prompt for a new session when resume fails: replay what happened so far."""
        hist = []
        plan = latest_plan(self.db, item["id"])
        if plan:
            hist.append(f"## Your latest plan (version {plan['version']})\n\n{plan['markdown']}")
        for q in self.db.all("SELECT * FROM qa_rounds WHERE item_id = ? AND answers_json IS NOT "
                             "NULL ORDER BY round", item["id"]):
            qs = "\n".join(f"- {x['id']}: {x['text']}" for x in json.loads(q["questions_json"]))
            ans = json.loads(q["answers_json"])
            hist.append(f"## Questions you asked (round {q['round']})\n\n{qs}\n\n"
                        f"Answers:\n\n{ans.get('text', '')}")
        for e in self.db.all("SELECT data_json FROM events WHERE item_id = ? AND kind = "
                             "'feedback' ORDER BY id", item["id"]):
            d = json.loads(e["data_json"])
            hist.append(f"## Reviewer feedback ({d.get('on', '')})\n\n{d.get('text', '')}")
        history = ("\n" + "\n\n".join(hist) + "\n") if hist else ""
        return render("fresh_session", item_id=item["id"], title=item["title"],
                      description=item["description"], history=history, prompt=prompt)

    # --- completion ----------------------------------------------------------------

    def _complete_plan(self, item: sqlite3.Row, proj: sqlite3.Row, out: dict) -> None:
        # one transaction; _guard publishes the change once it's committed
        item_id = item["id"]
        with self.db.tx():
            field = "roadmap_markdown" if item["type"] == "import" else "plan_markdown"
            md = (out.get(field) or "").strip()
            last = latest_plan(self.db, item_id)
            if md and (last is None or last["markdown"] != md):
                self.db.run("INSERT INTO plans (item_id, version, markdown, affected_files_json, "
                            "created_at) VALUES (?, ?, ?, ?, ?)", item_id,
                            (last["version"] + 1) if last else 1, md,
                            json.dumps(out.get("affected_files", [])), now())
            self._proposals(item, proj, out)
            summary = out.get("summary", "")
            if out["status"] == "blocked":
                it.transition(self.db, item_id, "failed", "claude", summary=summary,
                                 last_error=f"Claude is blocked: {out.get('blocked_reason')}")
            elif out["status"] == "needs_input" and out.get("questions"):
                rnd = self.db.one("SELECT COALESCE(MAX(round), 0) + 1 FROM qa_rounds "
                                  "WHERE item_id = ?", item_id)[0]
                self.db.run("INSERT INTO qa_rounds (item_id, round, questions_json) "
                            "VALUES (?, ?, ?)", item_id, rnd, json.dumps(out["questions"]))
                it.transition(self.db, item_id, "needs_input", "claude", summary=summary,
                                 last_error=None)
            elif md:
                it.transition(self.db, item_id, "plan_ready", "claude", summary=summary,
                                 last_error=None, stale_json=None)
            else:
                raise RunFailed(f"contract violation: status {out['status']} with no plan "
                                "or questions")

    def _complete_impl(self, item: sqlite3.Row, proj: sqlite3.Row, P: g.Project, kind: str,
                       out: dict, head_before: str | None) -> None:
        item_id = item["id"]
        wt = P.worktree(item_id)
        self._proposals(item, proj, out)
        if out["status"] == "blocked":
            self._transition(item_id, "failed", "claude", summary=out.get("summary", ""),
                             last_error=f"Claude is blocked: {out.get('blocked_reason')}")
            return
        if g.head(self.agent, wt) == head_before:
            raise RunFailed("contract violation: reported done but made no new commits")
        if not g.is_clean(self.agent, wt):
            it.event(self.db, item_id, "dirty_worktree", "system",
                     {"message": "uncommitted changes left after the run were not included"})
        g.transfer_branch(self.agent, P, item_id)
        if kind == "resolve_conflicts" and not g.is_ancestor(P, P.base, g.branch_name(item_id)):
            raise RunFailed(f"conflicts reported resolved but {P.base} isn't merged into the branch")
        g.push_branch(P, item_id)
        self._transition(item_id, "ready_to_test", "claude", summary=out.get("summary", ""),
                         reverify=int(kind == "resolve_conflicts"), last_error=None)

    def _proposals(self, item: sqlite3.Row, proj: sqlite3.Row, out: dict) -> None:
        """Store proposed items, skipping titles already proposed or on the roadmap."""
        known = {r[0].casefold() for r in self.db.all(
            "SELECT title FROM proposals WHERE project_id = ? UNION "
            "SELECT title FROM items WHERE project_id = ?", proj["id"], proj["id"])}
        for p in out.get("proposed_items") or []:
            if p["title"].casefold() in known:
                continue
            known.add(p["title"].casefold())
            self.db.run("INSERT INTO proposals (project_id, source_item_id, title, description, "
                        "reason) VALUES (?, ?, ?, ?, ?)", proj["id"], item["id"], p["title"],
                        p.get("description", ""), p.get("reason", ""))

    # --- merge -------------------------------------------------------------------

    def _merge(self, item_id: str) -> None:
        self._attempt(item_id)
        item = it.item(self.db, item_id)
        proj = it.project(self.db, item["project_id"])
        P = git_project(self.cfg, proj)
        out = latest_impl_output(self.db, item_id)
        if out is None:
            raise RunFailed("no successful implementation run to take the commit message from")
        g.ensure_canonical(P)  # it may not exist yet (e.g. after moving to a new VM)
        m = g.merge_local(P, item_id, out["commit_message"], strategy=proj["merge_strategy"])
        if not m.merged:
            self._transition(item_id, "implementing", data={"conflicts": m.conflicts})
            self._run(item_id, "resolve_conflicts", {"conflicts": m.conflicts or "(see git)"})
            return
        g.delete_remote_branch(P, item_id)
        g.remove_worktree(self.agent, P, item_id)
        self._transition(item_id, "merged", data={"commit": m.commit})


def _is_sudo(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].endswith(b"sudo")
    except OSError:
        return False


def _gzip(path: Path) -> None:
    if not path.exists():
        return
    with open(path, "rb") as src, gzip.open(f"{path}.gz", "ab") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
