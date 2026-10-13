"""Scheduler (§9): starts the next runs whenever there's room.

`tick()` is one pass over the unpaused projects. The phases (feedback, then
implementation, then planning) run across all projects in turn, so feedback
goes ahead of all other work. The rules that gate a single start
(dependencies, implementation slots, the global cap) live in pace/actions.py;
`tick` only adds ordering, `test_ahead` and `plan_ahead`. Before all of
them, it resumes the runs a restart interrupted (Runner.recover), in any
window.

Outside the run windows (pace/windows.py) `tick` starts nothing new; runs
already going finish (and interrupted ones resume). Human actions call pace/actions.py directly, so they bypass the
window.

`Scheduler` runs `tick` in a thread about every 10 s and whenever something is
published on the bus's `global` topic (every state change).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from pace import actions as act
from pace import items as it
from pace import windows
from pace.runs import Busy, Runner

log = logging.getLogger("pace.scheduler")

INTERVAL = 10.0
_SKIP = (act.ActionError, it.InvalidTransition, Busy)


def _count(r: Runner, project_id: int, *states: str) -> int:
    return r.db.one(f"SELECT COUNT(*) FROM items WHERE project_id = ? AND type = 'item' AND "
                    f"state IN ({', '.join('?' * len(states))})", project_id, *states)[0]


def _try(r: Runner, started: list[tuple[str, str]], item_id: str, kind: str, fn, **kw) -> bool:
    try:
        fn(r, item_id, **kw)
    except _SKIP as e:
        log.debug("not starting %s for %s: %s", kind, item_id, e)
        return False
    started.append((item_id, kind))
    return True


def _resume(r: Runner, p, started: list[tuple[str, str]]) -> None:
    """Runs a restart interrupted (Runner.recover): redo them, imports included."""
    for row in r.db.all("SELECT id FROM items WHERE project_id = ? AND state = 'failed' "
                        "ORDER BY type DESC, position, id", p["id"]):
        if not r.can_start():
            return
        if it.interrupted(r.db, row["id"]):
            _try(r, started, row["id"], "resume", act.retry, actor="system")


def _feedback(r: Runner, p, started: list[tuple[str, str]]) -> None:
    for row in it.items(r.db, p["id"], active_only=True):
        if not r.can_start():
            return
        if row["state"] == "changes_requested":
            _try(r, started, row["id"], "address_feedback", act.address_feedback)


def _implement(r: Runner, p, started: list[tuple[str, str]]) -> None:
    if _count(r, p["id"], "ready_to_test") >= p["test_ahead"]:
        return
    for row in it.items(r.db, p["id"], active_only=True):
        if not r.can_start():
            return
        if row["state"] == "approved" and not it.unmet_dependencies(r.db, row, "merged"):
            _try(r, started, row["id"], "implement", act.implement, actor="system")


def _plan(r: Runner, p, started: list[tuple[str, str]]) -> None:
    ahead = _count(r, p["id"], "planning", "needs_input", "plan_ready")
    for row in it.items(r.db, p["id"], active_only=True):
        if ahead >= p["plan_ahead"] or not r.can_start():
            return
        if row["state"] == "backlog" and not it.unmet_dependencies(r.db, row, "approved"):
            if _try(r, started, row["id"], "plan", act.plan, actor="system"):
                ahead += 1


def tick(r: Runner, now: datetime | None = None) -> list[tuple[str, str]]:
    """Start what §9 allows right now; returns the (item, kind) pairs started."""
    started: list[tuple[str, str]] = []
    projects = [p for p in it.projects(r.db) if not p["paused"]]
    phases = [_resume]  # interrupted runs were already going: they resume outside windows too
    if windows.is_open(r.cfg.run_windows, now):
        phases += [_feedback, _implement, _plan]
    for phase in phases:
        for p in projects:
            if not r.can_start():
                return started
            phase(r, p, started)
    return started


class Scheduler:
    def __init__(self, runner: Runner, interval: float = INTERVAL):
        self.runner, self.interval = runner, interval
        self._task: asyncio.Task | None = None
        self._q: asyncio.Queue | None = None

    def start(self) -> None:
        self._q = self.runner.bus.open("global")
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._q is not None:
            self.runner.bus.close(self._q)
            self._q = None

    async def _loop(self) -> None:
        assert self._q is not None
        was_open: bool | None = None
        while True:
            try:
                ws = windows.status(self.runner.cfg.run_windows)
                if was_open is not None and ws.open != was_open:
                    if ws.open:
                        log.info("run window opened")
                    else:
                        log.info("run window closed; next opens %s",
                                 ws.next_open.strftime("%a %H:%M") if ws.next_open else "?")
                was_open = ws.open
                started = await asyncio.to_thread(tick, self.runner)
                if started:
                    log.info("started %s", ", ".join(f"{k} {i}" for i, k in started))
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._q.get(), self.interval)
            except TimeoutError:
                pass
            while not self._q.empty():  # one tick per burst of changes
                self._q.get_nowait()
