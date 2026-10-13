"""The scheduler (§9): what a tick starts, and the loop waking on bus messages."""

import asyncio
import time
from datetime import datetime

import pytest

from pace import actions as act
from pace import items as it
from pace.runs import RATE_LIMIT_FALLBACK, backoff
from pace.scheduler import Scheduler, tick


def state(db, item_id):
    return it.item(db, item_id)["state"]


def set_state(db, item_id, s):
    db.run("UPDATE items SET state = ? WHERE id = ?", s, item_id)


def drain(runner):
    """Tick until nothing more starts, letting each run finish (one at a time)."""
    started = []
    while True:
        now = tick(runner)
        runner.wait()
        if not now:
            return started
        started += now


@pytest.fixture
def fake_start(runner, monkeypatch):
    """Record starts instead of running Claude; each recorded start holds a slot."""
    starts = []
    monkeypatch.setattr(runner, "start", lambda item_id, kind, inputs=None:
                        starts.append((item_id, kind)))
    monkeypatch.setattr(runner, "active", lambda: len(starts))
    return starts


def add(db, project, title, *deps):
    return it.add_item(db, project["id"], title, depends=list(deps))


def test_pipeline_flows_without_manual_starts(db, runner, cfg, scenario, project):
    cfg.concurrency = 1  # runs go one at a time, so scenario lines are used in order
    it.update_project(db, project["id"], plan_ahead=2, test_ahead=1)
    a = add(db, project, "A")
    b = add(db, project, "B", a)
    c = add(db, project, "C")
    d = add(db, project, "D", c)

    scenario("plan_ready", "plan_ready")
    assert drain(runner) == [(a, "plan"), (c, "plan")]  # B, D wait on approval
    assert [state(db, x) for x in (a, b, c, d)] == ["plan_ready", "backlog", "plan_ready",
                                                   "backlog"]

    scenario("implement_done", "plan_ready")
    act.approve(runner, a)
    assert drain(runner) == [(a, "implement"), (b, "plan")]
    assert state(db, a) == "ready_to_test" and state(db, b) == "plan_ready"

    scenario("plan_ready")
    act.approve(runner, c)
    assert drain(runner) == [(d, "plan")]  # C waits: test_ahead = 1 is reached
    assert state(db, c) == "approved"

    act.merge(runner, a)
    runner.wait()
    scenario("implement_done")
    assert drain(runner) == [(c, "implement")]

    act.approve(runner, b)
    act.approve(runner, d)
    assert drain(runner) == []  # C is waiting to be tested
    act.merge(runner, c)
    runner.wait()
    scenario("implement_done", "implement_done")
    assert drain(runner) == [(b, "implement")]  # roadmap order; D waits for test_ahead
    act.merge(runner, b)
    runner.wait()
    assert drain(runner) == [(d, "implement")]
    assert scenario.left() == []

    events = db.all("SELECT actor FROM events WHERE item_id = ? AND kind = 'state' AND "
                    "data_json LIKE '%\"to\": \"planning\"%'", a)
    assert [e["actor"] for e in events] == ["system"]


def test_dependencies_skip_not_block(db, runner, cfg, project, fake_start):
    it.update_project(db, project["id"], max_concurrent_implementations=3)
    x = add(db, project, "X")
    y = add(db, project, "Y", x)
    z = add(db, project, "Z")
    for i in (x, y, z):
        set_state(db, i, "approved")
    assert tick(runner) == [(x, "implement"), (z, "implement")]
    assert state(db, y) == "approved"


def test_feedback_goes_first_across_projects(db, runner, cfg, origin, project, fake_start):
    cfg.concurrency = 1
    add(db, project, "plan me")
    other = it.create_project(db, slug="ZED", name="Zed", origin=str(origin), base_branch="main")
    fix = add(db, other, "fix me")
    set_state(db, fix, "changes_requested")
    assert tick(runner) == [(fix, "address_feedback")]


def test_implement_before_plan(db, runner, cfg, project, fake_start):
    cfg.concurrency = 1
    add(db, project, "plan me")
    ok = add(db, project, "build me")
    set_state(db, ok, "approved")
    assert tick(runner) == [(ok, "implement")]


def test_ahead_limits(db, runner, project, fake_start):
    it.update_project(db, project["id"], plan_ahead=2, test_ahead=1)
    tested = add(db, project, "tested")
    set_state(db, tested, "ready_to_test")
    waiting = add(db, project, "approved")
    set_state(db, waiting, "approved")
    asked = add(db, project, "asked")
    set_state(db, asked, "needs_input")
    first, second = add(db, project, "first"), add(db, project, "second")
    assert tick(runner) == [(first, "plan")]  # needs_input + planning = plan_ahead
    assert state(db, second) == "backlog" and state(db, waiting) == "approved"
    assert tick(runner) == []


def test_global_cap(db, runner, cfg, project, fake_start):
    cfg.concurrency = 1
    a, b = add(db, project, "a"), add(db, project, "b")
    assert tick(runner) == [(a, "plan")]
    assert tick(runner) == []  # a holds the only slot
    assert state(db, b) == "backlog"


def test_paused_projects_and_imports_are_left_alone(db, runner, project, fake_start):
    it.add_import(db, project["id"])
    assert tick(runner) == []  # imports are started by hand only
    item = add(db, project, "a")
    it.update_project(db, project["id"], paused=1)
    assert tick(runner) == []
    it.update_project(db, project["id"], paused=0)
    assert tick(runner) == [(item, "plan")]


def test_loop_wakes_on_publish(db, runner, project, fake_start):
    async def main():
        runner.bus.attach(asyncio.get_running_loop())
        sched = Scheduler(runner, interval=60)
        sched.start()
        try:
            await asyncio.sleep(0.2)  # the first tick finds nothing
            assert fake_start == []
            item = add(db, project, "a")
            runner.publish("-")
            deadline = time.monotonic() + 5
            while not fake_start and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert fake_start == [(item, "plan")]
        finally:
            await sched.stop()

    asyncio.run(main())


def at(hh):
    return datetime(2026, 10, 10, hh, 0).astimezone()


def test_run_window_gates_the_scheduler(db, runner, cfg, project, fake_start):
    cfg.run_windows, cfg.concurrency = ["22:00-08:00"], 1
    a, b = add(db, project, "a"), add(db, project, "b")
    assert tick(runner, now=at(12)) == []
    assert tick(runner, now=at(23)) == [(a, "plan")]
    fake_start.clear()  # a's slot frees up
    assert tick(runner, now=at(3)) == [(b, "plan")]  # after midnight, still inside


def test_window_closing_leaves_runs_and_manual_starts_alone(db, runner, cfg, project,
                                                            fake_start):
    cfg.run_windows = ["22:00-08:00"]
    it.update_project(db, project["id"], plan_ahead=1)
    a, b, c = add(db, project, "a"), add(db, project, "b"), add(db, project, "c")
    assert tick(runner, now=at(23)) == [(a, "plan")]
    assert tick(runner, now=at(12)) == []  # closed: nothing new
    assert state(db, a) == "planning" and fake_start == [(a, "plan")]  # a keeps going
    act.plan(runner, c)  # Plan now bypasses the window
    assert fake_start == [(a, "plan"), (c, "plan")] and state(db, b) == "backlog"


def interrupted(db, runner, item_id, to, kind):
    it.transition(db, item_id, to, "human")
    db.run("INSERT INTO runs (item_id, kind, input_json, status) VALUES (?, ?, '{}', 'running')",
           item_id, kind)
    runner.recover()


def test_interrupted_runs_resume_first(db, runner, cfg, project, fake_start):
    cfg.concurrency = 1
    fix = add(db, project, "fix me")
    set_state(db, fix, "changes_requested")
    cut = add(db, project, "cut short")
    interrupted(db, runner, cut, "planning", "plan")
    assert tick(runner) == [(cut, "resume")]
    assert fake_start == [(cut, "plan")] and state(db, cut) == "planning"
    assert tick(runner) == []  # no double start while it runs


def test_resume_ignores_windows_but_not_pause(db, runner, cfg, project, fake_start):
    cfg.run_windows = ["22:00-08:00"]
    item = add(db, project, "a")
    imp = it.add_import(db, project["id"])
    interrupted(db, runner, item, "planning", "plan")
    interrupted(db, runner, imp, "planning", "import")
    it.update_project(db, project["id"], paused=1)
    assert tick(runner, now=at(23)) == []
    assert state(db, item) == "failed" and it.interrupted(db, item)
    it.update_project(db, project["id"], paused=0)
    assert tick(runner, now=at(12)) == [(item, "resume"), (imp, "resume")]  # imports too


def test_human_retry_wins(db, runner, project, fake_start):
    item = add(db, project, "a")
    interrupted(db, runner, item, "planning", "plan")
    act.retry(runner, item)
    assert tick(runner) == [] and fake_start == [(item, "plan")]


# --- rate-limit backoff (§8.1) ---------------------------------------------------

NOW = 1_800_000_000.0


def info(status="allowed", resets=None, five=None, seven=None, **extra):
    wins = {}
    if five is not None:
        wins["five_hour"] = {"utilization": five[0], "resetsAt": five[1]}
    if seven is not None:
        wins["seven_day"] = {"utilization": seven[0], "resetsAt": seven[1]}
    return {"status": status, "resetsAt": resets, "unifiedWindows": wins, **extra}


def test_backoff_rules():
    assert backoff(None, None, 0.9, NOW) is None
    assert backoff(info(five=(0.5, NOW + 100), seven=(0.3, NOW + 999)), None, 0.9, NOW) is None
    b = backoff(info("rejected", NOW + 600), None, 0.9, NOW)
    assert b.until == NOW + 600 and "rejected" in b.reason
    assert backoff(info("rejected", NOW - 1), None, 0.9, NOW) is None  # stale: over
    seen = datetime.fromtimestamp(NOW - 60).astimezone().isoformat()
    b = backoff(info("rejected", seen_at=seen), None, 0.9, NOW)
    assert b.until == NOW - 60 + RATE_LIMIT_FALLBACK
    b = backoff(info(five=(0.9, NOW + 100)), None, 0.9, NOW)
    assert b.until == NOW + 100 and "5-hour usage at 90%" in b.reason
    b = backoff(info(five=(0.2, NOW + 100), seven=(0.95, NOW + 9000)), None, 0.9, NOW)
    assert b.until == NOW + 9000 and "7-day" in b.reason
    b = backoff(info("rejected", NOW + 50, five=(0.99, NOW + 100)), None, 0.9, NOW)
    assert b.until == NOW + 100  # the latest end wins
    b = backoff(info(five=(0.95, None)), None, 0.9, NOW)
    assert b is None  # no reset time anywhere
    b = backoff(info(resets=NOW + 70, five=(0.95, None)), None, 0.9, NOW)
    assert b.until == NOW + 70  # falls back to the top-level resetsAt
    b = backoff(None, {"at": NOW - 10, "item": "TOY-001"}, 0.9, NOW)
    assert b.until == NOW - 10 + RATE_LIMIT_FALLBACK and "TOY-001" in b.reason
    assert backoff(None, {"at": NOW - RATE_LIMIT_FALLBACK - 1}, 0.9, NOW) is None
    for junk in ("x", [], {"status": None, "unifiedWindows": "?"},
                 {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": "hi"}}},
                 {"status": "rejected", "resetsAt": "soon", "seen_at": "yesterday"}):
        assert backoff(junk, {"at": "?"}, 0.9, NOW) is None


def test_backoff_gates_the_scheduler(db, runner, cfg, project, fake_start):
    a = add(db, project, "a")
    soon = time.time() + 600
    db.kv_set("rate_limit", info("rejected", soon))
    assert tick(runner) == []
    db.kv_set("rate_limit", info("rejected", time.time() - 1))  # reset has passed
    db.kv_set("rate_limit", info(five=(0.95, soon)))
    assert tick(runner) == []
    cfg.rate_limit_threshold = 0.99
    assert tick(runner) == [(a, "plan")]


def test_rate_limited_run_holds_the_scheduler(db, runner, cfg, scenario, project):
    a = add(db, project, "a")
    scenario("ratelimited -")
    tick(runner)
    runner.wait()
    assert state(db, a) == "backlog"
    assert tick(runner) == []  # no immediate restart loop
    db.run("DELETE FROM kv")  # the limit has reset
    scenario("plan_ready")
    assert drain(runner) == [(a, "plan")]
    assert state(db, a) == "plan_ready"


def test_resume_waits_out_a_backoff(db, runner, cfg, scenario, project):
    a = add(db, project, "a")
    interrupted(db, runner, a, "planning", "plan")
    db.kv_set("rate_limit", info("rejected", time.time() + 600))
    assert tick(runner) == []
    db.run("DELETE FROM kv")
    scenario("ratelimited -")  # the resumed run hits the limit itself
    assert tick(runner) == [(a, "resume")]
    runner.wait()
    assert state(db, a) == "failed" and it.interrupted(db, a)  # still queued to resume
    assert tick(runner) == []  # backing off
    db.run("DELETE FROM kv")
    scenario("plan_ready")
    assert drain(runner) == [(a, "resume")]
    assert state(db, a) == "plan_ready"
