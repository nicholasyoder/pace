"""What I can do to an item (§7). The web layer and tests both go through here.

Until the M2 scheduler exists, actions start the follow-up run directly when
there's capacity; otherwise the item waits (e.g. `approved`) with a button to
start it later.
"""

from __future__ import annotations

import json

from pace import items as it
from pace.db import now
from pace.runs import IMPL_KINDS, PLAN_KINDS, Runner


class ActionError(RuntimeError):
    pass


def _capacity(r: Runner) -> None:
    if not r.can_start():
        raise ActionError(f"all {r.cfg.concurrency} run slots are busy; try again when one finishes")


def _impl_slot_free(r: Runner, item_id: str) -> bool:
    item = it.item(r.db, item_id)
    proj = it.project(r.db, item["project_id"])
    n = r.db.one("SELECT COUNT(*) FROM items WHERE project_id = ? AND state IN "
                 "('implementing', 'merging')", proj["id"])[0]
    return n < proj["max_concurrent_implementations"] and r.can_start()


def plan(r: Runner, item_id: str) -> None:
    item = it.item(r.db, item_id)
    unmet = it.unmet_dependencies(r.db, item, "approved")
    if unmet:
        raise ActionError(f"waiting on {', '.join(unmet)} (dependencies must be approved first)")
    _capacity(r)
    it.transition(r.db, item_id, "planning", "human", expect="backlog")
    r.start(item_id, "plan")


def answer(r: Runner, item_id: str, answers: dict[str, str], decide_rest: bool = False) -> None:
    """answers: question id → my answer (an option id and/or free text)."""
    _capacity(r)
    with r.db.tx():
        rnd = r.db.one("SELECT * FROM qa_rounds WHERE item_id = ? ORDER BY round DESC LIMIT 1",
                       item_id)
        if rnd is None:
            raise ActionError("no open questions")
        lines = []
        for q in json.loads(rnd["questions_json"]):
            a = (answers.get(q["id"]) or "").strip()
            if not a:
                if not decide_rest:
                    raise ActionError(f"question {q['id']} has no answer "
                                      "(answer it or tick “let Claude decide the rest”)")
                a = f"no preference; use your recommended option ({q.get('recommended')})"
            lines.append(f"- {q['id']} ({q['text']}): {a}")
        text = "\n".join(lines)
        r.db.run("UPDATE qa_rounds SET answers_json = ?, answered_at = ? WHERE item_id = ? AND "
                 "round = ?", json.dumps({"answers": answers, "decide_rest": decide_rest,
                                          "text": text}), now(), item_id, rnd["round"])
        it.transition(r.db, item_id, "planning", "human", expect="needs_input",
                      data={"answers": text})
    r.start(item_id, "answers", {"answers": text})


def approve(r: Runner, item_id: str) -> None:
    it.transition(r.db, item_id, "approved", "human", expect="plan_ready")
    try:
        implement(r, item_id)
    except ActionError:
        pass  # stays approved; the item page offers "Implement"


def implement(r: Runner, item_id: str) -> None:
    item = it.item(r.db, item_id)
    unmet = it.unmet_dependencies(r.db, item, "merged")
    if unmet:
        raise ActionError(f"waiting on {', '.join(unmet)} (dependencies must be merged first)")
    if not _impl_slot_free(r, item_id):
        raise ActionError("no implementation slot free for this project")
    it.transition(r.db, item_id, "implementing", "human", expect="approved")
    r.start(item_id, "implement")


def revise_plan(r: Runner, item_id: str, feedback: str) -> None:
    feedback = feedback.strip()
    if not feedback:
        raise ActionError("feedback is empty")
    _capacity(r)
    with r.db.tx():
        it.event(r.db, item_id, "feedback", "human", {"on": "plan", "text": feedback})
        it.transition(r.db, item_id, "planning", "human", expect={"plan_ready", "approved"})
    r.start(item_id, "revise_plan", {"feedback": feedback})


def merge(r: Runner, item_id: str) -> None:
    item = it.item(r.db, item_id)
    if it.project(r.db, item["project_id"])["merge_mode"] != "local":
        raise ActionError("PR mode arrives in M3")
    _capacity(r)
    it.transition(r.db, item_id, "merging", "human", expect="ready_to_test")
    r.start_merge(item_id)


def problem(r: Runner, item_id: str, note: str) -> None:
    note = note.strip()
    if not note:
        raise ActionError("describe the problem")
    with r.db.tx():
        it.event(r.db, item_id, "feedback", "human", {"on": "implementation", "text": note})
        it.transition(r.db, item_id, "changes_requested", "human", expect="ready_to_test",
                      data={"note": note})
    try:
        address_feedback(r, item_id)
    except ActionError:
        pass  # waits in changes_requested


def address_feedback(r: Runner, item_id: str) -> None:
    if not _impl_slot_free(r, item_id):
        raise ActionError("no implementation slot free for this project")
    ev = r.db.one("SELECT data_json FROM events WHERE item_id = ? AND kind = 'feedback' "
                  "ORDER BY id DESC LIMIT 1", item_id)
    note = json.loads(ev["data_json"])["text"] if ev else ""
    it.transition(r.db, item_id, "implementing", "system", expect="changes_requested")
    r.start(item_id, "address_feedback", {"feedback": note})


def park(r: Runner, item_id: str) -> None:
    it.transition(r.db, item_id, "parked", "human",
                  expect={"backlog", "needs_input", "plan_ready", "approved", "ready_to_test",
                          "changes_requested", "failed"})


def unpark(r: Runner, item_id: str) -> None:
    it.transition(r.db, item_id, "backlog", "human", expect="parked")


def to_backlog(r: Runner, item_id: str) -> None:
    """From failed: start over (e.g. after editing the description)."""
    it.transition(r.db, item_id, "backlog", "human", expect="failed")


def retry(r: Runner, item_id: str) -> None:
    """From failed: redo what failed — the same run kind with the same inputs."""
    frm = it.failed_from(r.db, item_id)
    _capacity(r)
    if frm == "merging":
        it.transition(r.db, item_id, "merging", "human", expect="failed")
        r.start_merge(item_id)
        return
    kinds = PLAN_KINDS if frm == "planning" else IMPL_KINDS if frm == "implementing" else None
    if kinds is None:
        raise ActionError(f"nothing to retry (failed from {frm})")
    last = r.db.one(f"SELECT kind, input_json FROM runs WHERE item_id = ? AND kind IN "
                    f"({', '.join('?' * len(kinds))}) ORDER BY id DESC LIMIT 1", item_id, *kinds)
    if last is None:
        raise ActionError("no earlier run to retry")
    it.transition(r.db, item_id, frm, "human", expect="failed", data={"retry": last["kind"]})
    r.start(item_id, last["kind"], json.loads(last["input_json"]))


def cancel(r: Runner, item_id: str) -> None:
    if not r.cancel(item_id):
        raise ActionError("no run in progress")
    it.event(r.db, item_id, "cancel", "human", {})
