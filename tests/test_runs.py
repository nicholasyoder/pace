"""Item flows through actions + runner against fake_claude, as the current user."""

import gzip
import json
from pathlib import Path

import pytest

from pace import actions as act
from pace import items as it
from pace import git as g
from pace.runs import git_project, latest_impl_output
from pace.scheduler import tick
from tests.conftest import push_to_origin, sh


def state(db, item_id):
    return it.item(db, item_id)["state"]


def runs(db, item_id):
    return [(r["kind"], r["status"]) for r in
            db.all("SELECT * FROM runs WHERE item_id = ? ORDER BY id", item_id)]


def calls(tmp_path):
    return [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]


def go(runner, fn, *args):
    fn(runner, *args)
    runner.wait()


@pytest.fixture
def item(db, project):
    return it.add_item(db, project["id"], "Greet many", "Support greeting multiple people.")


def test_plan_answer_implement_merge(tmp_path, db, runner, scenario, origin, item):
    scenario("plan_needs_input", "plan_ready", "implement_done")
    go(runner, act.plan, item)
    assert state(db, item) == "needs_input"
    rnd = db.one("SELECT * FROM qa_rounds WHERE item_id = ?", item)
    assert [q["id"] for q in json.loads(rnd["questions_json"])] == ["q1_format"]

    with pytest.raises(act.ActionError, match="no answer"):
        act.answer(runner, item, {})
    go(runner, act.answer, item, {"q1_format": "b — keep it simple"})
    assert state(db, item) == "plan_ready"
    assert db.one("SELECT COUNT(*) FROM plans WHERE item_id = ?", item)[0] == 2

    act.approve(runner, item)  # only approves; the scheduler (or Implement) starts it
    assert state(db, item) == "approved"
    go(runner, act.implement, item)
    row = it.item(db, item)
    assert row["state"] == "ready_to_test" and row["reverify"] == 0 and row["attempts"] == 0
    assert sh("git", "branch", "--list", "pace/TOY-001", cwd=origin)  # pushed for testing

    go(runner, act.merge, item)
    assert state(db, item) == "merged"
    assert sh("git", "log", "--format=%s", "main", cwd=origin).splitlines() == \
        ["TOY-002: greet multiple people", "init"]
    assert sh("git", "branch", "--list", "pace/*", cwd=origin) == ""
    assert not Path(runner.cfg.agent_home / "work/toy/wt" / item).exists()

    assert runs(db, item) == [("plan", "succeeded"), ("answers", "succeeded"),
                              ("implement", "succeeded")]
    costs = [r["cost_usd"] for r in db.all("SELECT cost_usd FROM runs ORDER BY id")]
    assert costs == pytest.approx([0.2374174, 0.0355976, 0.2862412])  # per-run deltas

    c = calls(tmp_path)
    assert "--resume" not in c[0]["argv"] and "--permission-mode" in c[0]["argv"]
    assert c[1]["argv"][c[1]["argv"].index("--resume") + 1] == "0a607ce7-5920-466c-95a2-73a43f80ec7c"
    assert "--dangerously-skip-permissions" in c[2]["argv"]
    assert "keep it simple" in c[1]["argv"][c[1]["argv"].index("-p") + 1]

    log = db.one("SELECT log_path FROM runs WHERE kind = 'plan'")["log_path"]
    with gzip.open(log, "rt") as f:
        assert json.loads(f.read().splitlines()[-1])["type"] == "result"
    assert db.kv_get("rate_limit")["status"] == "allowed"
    assert scenario.left() == []


def test_usage_is_stored_and_published_as_it_arrives(db, runner, scenario, item, monkeypatch):
    seen = []
    publish = runner.bus.publish

    def record(topic, msg):
        if msg.get("type") == "usage":
            info = db.kv_get("rate_limit")
            seen.append((topic, info["unifiedWindows"]["five_hour"]["utilization"],
                         db.one("SELECT status FROM runs ORDER BY id DESC")["status"]))
        publish(topic, msg)

    monkeypatch.setattr(runner.bus, "publish", record)
    scenario("plan_needs_input")
    go(runner, act.plan, item)
    # stored mid-run (the run is still `running`), and only published when the number moved
    assert seen[:2] == [("global", 0.08, "running"), (f"item:{item}", 0.08, "running")]
    fives = [u for t, u, _ in seen if t == "global"]
    assert fives == [0.08, 0.09]
    assert db.kv_get("rate_limit")["seen_at"]


def test_problem_then_address_feedback(tmp_path, db, runner, scenario, origin, item):
    scenario("plan_ready", "implement_done",
             'implement_done {"summary": "fixed it", "manual_checks": ["look again"]}')
    go(runner, act.plan, item)
    act.approve(runner, item)
    go(runner, act.implement, item)
    first = sh("git", "rev-parse", "pace/TOY-001", cwd=origin)
    act.problem(runner, item, "it greets nobody")
    assert state(db, item) == "changes_requested"
    go(runner, act.address_feedback, item)
    assert state(db, item) == "ready_to_test"
    assert sh("git", "rev-parse", "pace/TOY-001~1", cwd=origin) == first  # added on top
    assert latest_impl_output(db, item)["manual_checks"] == ["look again"]
    prompt = calls(tmp_path)[-1]["argv"]
    assert "it greets nobody" in prompt[prompt.index("-p") + 1]


def test_merge_conflict_resolved_by_claude(tmp_path, db, runner, scenario, origin, item):
    scenario("plan_ready", "implement_done", "resolve implement_done")
    go(runner, act.plan, item)
    act.approve(runner, item)
    go(runner, act.implement, item)
    push_to_origin(tmp_path, origin, "fake_change.txt", "someone else's version\n")

    go(runner, act.merge, item)
    row = it.item(db, item)
    assert row["state"] == "ready_to_test" and row["reverify"] == 1
    assert runs(db, item)[-1] == ("resolve_conflicts", "succeeded")
    prompt = calls(tmp_path)[-1]["argv"]
    assert "fake_change.txt" in prompt[prompt.index("-p") + 1]

    go(runner, act.merge, item)
    assert state(db, item) == "merged"


def test_invalid_output_retries_once(tmp_path, db, runner, scenario, item):
    scenario("invalid plan_ready", "plan_ready")
    go(runner, act.plan, item)
    assert state(db, item) == "plan_ready"
    assert len(calls(tmp_path)) == 2
    assert [e["kind"] for e in it.events(db, item)].count("run_retry") == 1


def test_invalid_twice_fails_then_retry(tmp_path, db, runner, scenario, item):
    scenario("invalid plan_ready", "invalid plan_ready", "plan_ready")
    go(runner, act.plan, item)
    row = it.item(db, item)
    assert row["state"] == "failed" and "failed twice" in row["last_error"]
    assert runs(db, item) == [("plan", "failed")]
    go(runner, act.retry, item)
    assert state(db, item) == "plan_ready"


def test_blocked(db, runner, scenario, item):
    scenario('plan_ready {"status": "blocked", "blocked_reason": "needs a design decision"}')
    go(runner, act.plan, item)
    row = it.item(db, item)
    assert row["state"] == "failed" and "needs a design decision" in row["last_error"]


def test_attempt_cap(db, runner, scenario, item):
    blocked = 'plan_ready {"status": "blocked", "blocked_reason": "flaky"}'
    scenario(blocked, blocked, blocked, "plan_ready")
    go(runner, act.plan, item)
    for _ in range(2):  # what an automatic re-queue does
        it.transition(db, item, "planning", "system", expect="failed")
        runner.start(item, "plan")
        runner.wait()
    assert it.item(db, item)["attempts"] == 3
    it.transition(db, item, "planning", "system", expect="failed")
    runner.start(item, "plan")
    runner.wait()
    row = it.item(db, item)
    assert row["state"] == "failed"
    assert "gave up after 3 attempts in planning; last error: " in row["last_error"]
    assert "flaky" in row["last_error"]
    assert len(runs(db, item)) == 3 and scenario.left() == ["plan_ready"]  # no 4th call
    go(runner, act.retry, item)
    row = it.item(db, item)
    assert row["state"] == "plan_ready" and row["attempts"] == 0


def test_done_without_commits_is_a_violation(db, runner, scenario, item):
    scenario("plan_ready", "nocommit implement_done", "implement_done")
    go(runner, act.plan, item)
    act.approve(runner, item)
    go(runner, act.implement, item)
    row = it.item(db, item)
    assert row["state"] == "failed" and "no new commits" in row["last_error"]
    go(runner, act.retry, item)
    assert state(db, item) == "ready_to_test"


def test_revise_plan_and_proposals(db, runner, scenario, project, item):
    prop = {"title": "Add tests", "description": "d", "reason": "r"}
    scenario(f'plan_ready {json.dumps({"proposed_items": [prop]})}',
             f'plan_ready {json.dumps({"proposed_items": [prop], "plan_markdown": "v2"})}')
    go(runner, act.plan, item)
    go(runner, act.revise_plan, item, "smaller please")
    assert state(db, item) == "plan_ready"
    assert [r["version"] for r in db.all("SELECT version FROM plans")] == [1, 2]
    assert db.one("SELECT COUNT(*) FROM proposals")[0] == 1  # deduped by title
    pid = db.one("SELECT id, created_at FROM proposals")
    assert pid["created_at"]
    it.dismiss_proposal(db, pid["id"])
    scenario(f'plan_ready {json.dumps({"proposed_items": [dict(prop, title="ADD TESTS")]})}')
    go(runner, act.revise_plan, item, "again")
    assert db.one("SELECT COUNT(*) FROM proposals")[0] == 1  # dismissed titles stay out
    act.park(runner, item)
    act.unpark(runner, item)
    assert state(db, item) == "backlog"


def test_dependencies_gate_and_context(tmp_path, db, runner, scenario, project, item):
    b = it.add_item(db, project["id"], "Follow-up", depends=[item])
    with pytest.raises(act.ActionError, match="waiting on TOY-001"):
        act.plan(runner, b)
    scenario("plan_ready", "plan_ready")
    go(runner, act.plan, item)
    act.approve(runner, item)
    go(runner, act.plan, b)
    prompt = calls(tmp_path)[-1]["argv"]
    assert "### TOY-001: Greet many" in prompt[prompt.index("-p") + 1]


def interrupt(db, item_id, to, kind=None, inputs=None):
    """What a restart leaves behind: the item busy, its run still `running`."""
    it.transition(db, item_id, to, "human")
    if kind:
        db.run("INSERT INTO runs (item_id, kind, input_json, status, pid) "
               "VALUES (?, ?, ?, 'running', 999999)", item_id, kind, json.dumps(inputs or {}))


def resumes(db, item_id):
    return [(e["actor"], json.loads(e["data_json"]).get("retry"))
            for e in it.events(db, item_id) if e["kind"] == "state"
            and json.loads(e["data_json"]).get("resume")]


def test_recover_after_restart(db, runner, item):
    interrupt(db, item, "planning", "plan")
    runner.recover()
    row = it.item(db, item)
    assert row["state"] == "failed" and "resumes automatically" in row["last_error"]
    assert it.interrupted(db, item)
    assert db.one("SELECT status FROM runs")["status"] == "interrupted"
    act.to_backlog(runner, item)  # anything else happening clears the flag
    assert not it.interrupted(db, item)


def test_recover_without_its_run_just_fails(db, runner, item):
    """Stopped between the transition and the run starting: Retry would redo the wrong run."""
    interrupt(db, item, "planning", "plan")
    db.run("UPDATE runs SET status = 'succeeded'")
    it.transition(db, item, "needs_input", "claude")
    interrupt(db, item, "planning")  # answers given, but no `answers` run yet
    runner.recover()
    row = it.item(db, item)
    assert row["state"] == "failed" and not it.interrupted(db, item)
    assert "resumes" not in row["last_error"]
    assert tick(runner) == []


def test_restart_resumes_each_kind(tmp_path, db, runner, scenario, origin, item):
    scenario("plan_needs_input", "plan_ready", "plan_ready", "implement_done")
    go(runner, act.plan, item)

    interrupt(db, item, "planning", "answers", {"answers": "- q1_format: b"})
    runner.recover()
    assert tick(runner) == [(item, "resume")]
    runner.wait()
    assert state(db, item) == "plan_ready"
    last = calls(tmp_path)[-1]["argv"]
    assert "--resume" in last and "q1_format: b" in last[last.index("-p") + 1]

    interrupt(db, item, "planning", "plan")
    runner.recover()
    tick(runner)
    runner.wait()
    assert state(db, item) == "plan_ready"

    act.approve(runner, item)
    interrupt(db, item, "implementing", "implement")
    runner.recover()
    tick(runner)
    runner.wait()
    assert state(db, item) == "ready_to_test"

    interrupt(db, item, "merging")
    runner.recover()
    tick(runner)
    runner.wait()
    assert state(db, item) == "merged" and scenario.left() == []
    assert resumes(db, item) == [("system", "answers"), ("system", "plan"),
                                 ("system", "implement"), ("system", None)]
    kinds = [k for k, s in runs(db, item) if s == "interrupted"]
    assert kinds == ["answers", "plan", "implement"]


def test_resumed_merge_after_its_push_landed(db, runner, scenario, origin, item):
    scenario("plan_ready", "implement_done")
    go(runner, act.plan, item)
    act.approve(runner, item)
    go(runner, act.implement, item)
    proj = it.project(db, it.item(db, item)["project_id"])
    msg = latest_impl_output(db, item)["commit_message"]
    assert g.merge_local(git_project(runner.cfg, proj), item, msg).merged  # then PACE died
    interrupt(db, item, "merging")
    runner.recover()
    tick(runner)
    runner.wait()
    assert state(db, item) == "merged"
    assert len(sh("git", "log", "--format=%s", "main", cwd=origin).splitlines()) == 2


def test_resume_counts_toward_the_attempt_cap(db, runner, item):
    interrupt(db, item, "planning", "plan")
    it.update(db, item, attempts=3)
    runner.recover()
    tick(runner)
    runner.wait()
    row = it.item(db, item)
    assert row["state"] == "failed" and "gave up after 3 attempts in planning" in row["last_error"]
    assert not it.interrupted(db, item)


ROADMAP = ("Order is a suggestion.\n\n# Tier 2\nRealism.\n\n## TOY-001: Shadows\n\n"
           "## TOY-002: Skybox\n- depends: TOY-001\n- kind: debt\n\n# Someday\n\n## TOY-003: Net\n- parked\n")


def test_import_flow(tmp_path, db, runner, scenario, project):
    q = {"id": "q1", "text": "Split rendering polish?", "why_it_matters": "granularity",
         "options": [{"id": "a", "label": "Split", "description": "one per feature"}],
         "recommended": "a", "allow_free_text": True}
    scenario(
        f'plan_needs_input {json.dumps({"questions": [q], "roadmap_markdown": "draft"})}',
        # invalid on purpose: unknown dependency → one retry with the parser's errors
        "plan_ready " + json.dumps({"roadmap_markdown": "## TOY-001: x\n- depends: TOY-050\n"}),
        f'plan_ready {json.dumps({"roadmap_markdown": ROADMAP, "notes": "Read docs/roadmap."})}',
    )
    imp = act.start_import(runner, project["id"], "Look in docs/roadmap.")
    runner.wait()
    assert state(db, imp) == "needs_input"
    first = calls(tmp_path)[0]["argv"]
    prompt = first[first.index("-p") + 1]
    assert "Look in docs/roadmap." in prompt and "`TOY-001`, `TOY-002`" in prompt
    assert "--permission-mode" in first and "roadmap_markdown" in first[first.index("--json-schema") + 1]
    assert it.items(db, project["id"]) == []  # the import isn't a roadmap item

    go(runner, act.answer, imp, {"q1": "a"})
    assert state(db, imp) == "plan_ready"
    retry = calls(tmp_path)[-1]["argv"]
    assert "unknown dependency TOY-050" in retry[retry.index("-p") + 1]

    go(runner, act.approve, imp)
    assert state(db, imp) == "merged"
    rows = it.items(db, project["id"])
    assert [(r["id"], r["title"], r["group_title"], r["kind"], r["state"]) for r in rows] == [
        ("TOY-001", "Shadows", "Tier 2", "", "backlog"),
        ("TOY-002", "Skybox", "Tier 2", "debt", "backlog"),
        ("TOY-003", "Net", "Someday", "", "parked")]
    assert it.project(db, project["id"])["preamble"] == "Order is a suggestion."

    # planning an imported item gets the roadmap and group context
    scenario("plan_ready")
    go(runner, act.plan, "TOY-001")
    prompt = calls(tmp_path)[-1]["argv"]
    text = prompt[prompt.index("-p") + 1]
    assert "Order is a suggestion." in text and "## Part of: Tier 2\n\nRealism." in text


def test_moved_to_new_vm(tmp_path, db, runner, scenario, origin, item):
    """Worktree and Claude session gone (as after a move): approve still works."""
    import shutil
    scenario("plan_ready", "noresume -", "implement_done")
    go(runner, act.plan, item)
    shutil.rmtree(runner.cfg.agent_home)            # the agent's clone and worktrees
    shutil.rmtree(runner.cfg.data_dir / "repos")    # canonical repos re-clone themselves
    act.approve(runner, item)
    go(runner, act.implement, item)
    assert state(db, item) == "ready_to_test"
    c = calls(tmp_path)
    assert "--resume" in c[1]["argv"] and "--resume" not in c[2]["argv"]  # fresh session
    fresh = c[2]["argv"][c[2]["argv"].index("-p") + 1]
    assert "could not be resumed" in fresh and "Your latest plan" in fresh
    assert "worktree_recreated" in [e["kind"] for e in it.events(db, item)]
    shutil.rmtree(runner.cfg.data_dir / "repos")
    go(runner, act.merge, item)
    assert state(db, item) == "merged"


# --- rate limits (§8.1): the item goes back to where it was --------------------

def limited(db, runner, item, back_in):
    row = it.item(db, item)
    assert row["state"] == back_in and row["last_error"].startswith("rate limited; back in")
    assert runs(db, item)[-1][1] == "rate_limited"
    assert db.kv_get("rate_limit_hit")["item"] == item
    assert db.kv_get("rate_limit")["status"] == "rejected"
    assert runner.backoff() is not None


def test_rate_limited_plan_goes_back_to_backlog(db, runner, scenario, item):
    scenario("ratelimited -", "plan_ready")
    go(runner, act.plan, item)
    limited(db, runner, item, "backlog")
    go(runner, act.plan, item)  # picked up again later
    assert state(db, item) == "plan_ready"


def test_rate_limited_implement_and_feedback(db, runner, scenario, item):
    scenario("plan_ready", "ratelimited -", "implement_done", "ratelimited -", "implement_done")
    go(runner, act.plan, item)
    act.approve(runner, item)
    go(runner, act.implement, item)
    limited(db, runner, item, "approved")
    go(runner, act.implement, item)
    assert state(db, item) == "ready_to_test"
    act.problem(runner, item, "still broken")
    go(runner, act.address_feedback, item)
    limited(db, runner, item, "changes_requested")
    go(runner, act.address_feedback, item)
    assert state(db, item) == "ready_to_test"


def test_rate_limited_answers_and_revise(db, runner, scenario, item):
    scenario("plan_needs_input", "ratelimited -", "plan_ready", "ratelimited -")
    go(runner, act.plan, item)
    go(runner, act.answer, item, {}, True)
    limited(db, runner, item, "needs_input")
    go(runner, act.answer, item, {}, True)  # the open round can be answered again
    assert state(db, item) == "plan_ready"
    act.approve(runner, item)
    go(runner, act.revise_plan, item, "smaller please")
    limited(db, runner, item, "approved")


def test_rate_limited_conflict_resolution(tmp_path, db, runner, scenario, origin, item):
    scenario("plan_ready", "implement_done", "ratelimited -", "resolve implement_done")
    go(runner, act.plan, item)
    act.approve(runner, item)
    go(runner, act.implement, item)
    push_to_origin(tmp_path, origin, "fake_change.txt", "someone else's version\n")
    go(runner, act.merge, item)
    limited(db, runner, item, "ready_to_test")
    go(runner, act.merge, item)
    assert state(db, item) == "ready_to_test" and it.item(db, item)["reverify"] == 1


def test_rate_limited_retry_stays_failed(db, runner, scenario, item):
    scenario("invalid plan_ready", "invalid plan_ready", "ratelimited -", "plan_ready")
    go(runner, act.plan, item)
    assert state(db, item) == "failed"
    go(runner, act.retry, item)
    row = it.item(db, item)
    assert row["state"] == "failed" and "rate limited" in row["last_error"]
    assert it.failed_from(db, item) == "planning"
    go(runner, act.retry, item)
    assert state(db, item) == "plan_ready"
