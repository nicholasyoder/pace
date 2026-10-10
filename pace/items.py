"""Projects, items and the item state machine (§7).

Every state change goes through `transition()`: one transaction that checks
the move is allowed, updates the item and appends an `events` row.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from pace import roadmap
from pace.db import DB, now

STATES = ["backlog", "planning", "needs_input", "plan_ready", "approved", "implementing",
          "ready_to_test", "changes_requested", "merging", "merged", "failed", "parked"]

# states where a run or merge is in progress; the item can't be parked or edited into them
BUSY = {"planning", "implementing", "merging"}
INACTIVE = {"merged", "parked"}
INBOX = ["needs_input", "plan_ready", "ready_to_test", "failed"]

ALLOWED: dict[str, set[str]] = {
    "backlog": {"planning", "parked"},
    "planning": {"needs_input", "plan_ready", "failed"},
    "needs_input": {"planning", "parked"},
    "plan_ready": {"approved", "planning", "parked"},
    "approved": {"implementing", "planning", "parked"},
    "implementing": {"ready_to_test", "failed"},
    "ready_to_test": {"merging", "changes_requested", "parked"},
    "changes_requested": {"implementing", "parked"},
    "merging": {"merged", "implementing", "ready_to_test", "failed"},
    "merged": set(),
    "failed": {"planning", "implementing", "merging", "backlog", "parked"},
    "parked": {"backlog"},
}

PROJECT_FIELDS = ["slug", "name", "origin", "base_branch", "merge_mode", "merge_strategy",
                  "plan_ahead", "test_ahead", "max_concurrent_implementations", "model",
                  "extra_instructions", "paused"]
ITEM_FIELDS = {"session_id", "branch", "plan_base_commit", "impl_base_commit", "pr_url",
               "stale_json", "reverify", "summary", "last_error", "attempts"}


class InvalidTransition(RuntimeError):
    pass


class NotFound(LookupError):
    pass


# --- projects ---------------------------------------------------------------

def create_project(db: DB, **fields: Any) -> sqlite3.Row:
    bad = set(fields) - set(PROJECT_FIELDS)
    if bad:
        raise ValueError(f"unknown project fields {sorted(bad)}")
    slug = fields.get("slug", "")
    if not slug or not roadmap.ID_RE.match(f"{slug}-001"):
        raise ValueError("slug must be uppercase letters/digits, starting with a letter")
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    with db.tx():
        cur = db.run(f"INSERT INTO projects ({cols}) VALUES ({marks})", *fields.values())
    return project(db, cur.lastrowid)


def update_project(db: DB, project_id: int, **fields: Any) -> None:
    bad = set(fields) - (set(PROJECT_FIELDS) - {"slug"})
    if bad:
        raise ValueError(f"can't update {sorted(bad)}")
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.run(f"UPDATE projects SET {sets} WHERE id = ?", *fields.values(), project_id)


def project(db: DB, ref: int | str) -> sqlite3.Row:
    col = "id" if isinstance(ref, int) else "slug"
    row = db.one(f"SELECT * FROM projects WHERE {col} = ?", ref)
    if row is None:
        raise NotFound(f"project {ref}")
    return row


def projects(db: DB) -> list[sqlite3.Row]:
    return db.all("SELECT * FROM projects ORDER BY name")


# --- items ------------------------------------------------------------------

def item(db: DB, item_id: str) -> sqlite3.Row:
    row = db.one("SELECT * FROM items WHERE id = ?", item_id)
    if row is None:
        raise NotFound(f"item {item_id}")
    return row


def items(db: DB, project_id: int, active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM items WHERE project_id = ?"
    if active_only:
        sql += " AND state NOT IN ('merged', 'parked')"
    return db.all(sql + " ORDER BY position, created_at", project_id)


def depends(row: sqlite3.Row) -> list[str]:
    return json.loads(row["depends_json"])


def _next_id(db: DB, p: sqlite3.Row) -> str:
    row = db.one("UPDATE projects SET next_id = next_id + 1 WHERE id = ? RETURNING next_id - 1",
                 p["id"])
    return roadmap.format_id(p["slug"], row[0])


def _check_depends(db: DB, p: sqlite3.Row, deps: Iterable[str], self_id: str | None) -> None:
    for d in deps:
        if d == self_id:
            raise ValueError(f"{d} can't depend on itself")
        if db.one("SELECT 1 FROM items WHERE id = ? AND project_id = ?", d, p["id"]) is None:
            raise ValueError(f"unknown dependency {d}")


def add_item(db: DB, project_id: int, title: str, description: str = "",
             depends: list[str] | None = None, top: bool = False) -> str:
    title = title.strip()
    if not title:
        raise ValueError("title is required")
    with db.tx():
        p = project(db, project_id)
        _check_depends(db, p, depends or [], None)
        item_id = _next_id(db, p)
        if top:
            db.run("UPDATE items SET position = position + 1 WHERE project_id = ?", p["id"])
            pos = 0
        else:
            pos = db.one("SELECT COALESCE(MAX(position) + 1, 0) FROM items WHERE project_id = ?",
                         p["id"])[0]
        ts = now()
        db.run("INSERT INTO items (id, project_id, position, title, description, depends_json, "
               "state_since, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               item_id, p["id"], pos, title, description.strip(), json.dumps(depends or []),
               ts, ts, ts)
        event(db, item_id, "created", "human", {"title": title})
    return item_id


def edit_item(db: DB, item_id: str, title: str, description: str, depends: list[str]) -> None:
    title = title.strip()
    if not title:
        raise ValueError("title is required")
    with db.tx():
        it = item(db, item_id)
        _check_depends(db, project(db, it["project_id"]), depends, item_id)
        db.run("UPDATE items SET title = ?, description = ?, depends_json = ?, updated_at = ? "
               "WHERE id = ?", title, description.strip(), json.dumps(depends), now(), item_id)
        event(db, item_id, "edited", "human", {})


def move(db: DB, item_id: str, where: str) -> None:
    """Reorder among the project's active items: up | down | top."""
    with db.tx():
        it = item(db, item_id)
        order = [r["id"] for r in items(db, it["project_id"], active_only=True)]
        if item_id not in order:
            return
        i = order.index(item_id)
        order.pop(i)
        j = {"up": max(i - 1, 0), "down": min(i + 1, len(order)), "top": 0}[where]
        order.insert(j, item_id)
        _renumber(db, it["project_id"], order)


def _renumber(db: DB, project_id: int, active_order: list[str]) -> None:
    rest = [r["id"] for r in items(db, project_id) if r["id"] not in set(active_order)]
    for pos, iid in enumerate(active_order + rest):
        db.run("UPDATE items SET position = ? WHERE id = ?", pos, iid)


# --- roadmap markdown ---------------------------------------------------------

def active_markdown(db: DB, project_id: int) -> str:
    return roadmap.serialize([
        roadmap.Entry(r["title"], r["id"], depends(r), r["description"])
        for r in items(db, project_id, active_only=True)])


class MissingItems(Exception):
    """The save leaves out active items; ask whether to park them."""

    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(", ".join(ids))


def save_markdown(db: DB, project_id: int, text: str, park_missing: bool = False) -> list[str]:
    """Apply an edited active roadmap. Returns the IDs of newly created items.

    Raises RoadmapError for parse/validation problems (nothing saved), or
    MissingItems when active items are left out and park_missing is False.
    """
    p = project(db, project_id)
    entries = roadmap.parse(text, p["slug"])
    with db.tx():
        active = {r["id"]: r for r in items(db, project_id, active_only=True)}
        errors = []
        for e in entries:
            if e.id and e.id not in active:
                row = db.one("SELECT state FROM items WHERE id = ? AND project_id = ?", e.id,
                             project_id)
                why = f"is {row['state']}" if row else "is unknown (remove the ID to add a new item)"
                errors.append(roadmap.ParseError(e.line, f"{e.id} {why}"))
            for d in e.depends:
                if db.one("SELECT 1 FROM items WHERE id = ? AND project_id = ?", d,
                          project_id) is None:
                    errors.append(roadmap.ParseError(e.line, f"unknown dependency {d}"))
        if errors:
            raise roadmap.RoadmapError(errors)
        missing = [iid for iid in active if iid not in {e.id for e in entries}]
        busy = [iid for iid in missing if active[iid]["state"] in BUSY]
        if busy:
            raise roadmap.RoadmapError([roadmap.ParseError(
                0, f"{', '.join(busy)} can't be removed while a run or merge is in progress")])
        if missing and not park_missing:
            raise MissingItems(missing)

        created, order = [], []
        ts = now()
        for e in entries:
            if e.id is None:
                e.id = _next_id(db, p)
                db.run("INSERT INTO items (id, project_id, position, title, description, "
                       "depends_json, state_since, created_at, updated_at) "
                       "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)",
                       e.id, project_id, e.title, e.description, json.dumps(e.depends), ts, ts, ts)
                event(db, e.id, "created", "human", {"title": e.title})
                created.append(e.id)
            else:
                old = active[e.id]
                if (old["title"], old["description"], depends(old)) != \
                        (e.title, e.description, e.depends):
                    db.run("UPDATE items SET title = ?, description = ?, depends_json = ?, "
                           "updated_at = ? WHERE id = ?",
                           e.title, e.description, json.dumps(e.depends), ts, e.id)
                    event(db, e.id, "edited", "human", {})
            order.append(e.id)
        for iid in missing:
            transition(db, iid, "parked", "human", data={"reason": "removed from roadmap"})
        _renumber(db, project_id, order)
    return created


# --- state machine --------------------------------------------------------------

def event(db: DB, item_id: str | None, kind: str, actor: str, data: dict | None = None) -> None:
    db.run("INSERT INTO events (item_id, ts, kind, actor, data_json) VALUES (?, ?, ?, ?, ?)",
           item_id, now(), kind, actor, json.dumps(data or {}))


def transition(db: DB, item_id: str, to: str, actor: str, *, expect: str | set[str] | None = None,
               data: dict | None = None, **fields: Any) -> sqlite3.Row:
    """Move an item to `to`, optionally updating ITEM_FIELDS, and log it.

    `expect` guards against acting on a stale page (e.g. a double-clicked
    Approve): the item must currently be in that state / one of those states.
    """
    bad = set(fields) - ITEM_FIELDS
    if bad:
        raise ValueError(f"unknown item fields {sorted(bad)}")
    with db.tx():
        it = item(db, item_id)
        frm = it["state"]
        if expect is not None and frm not in ({expect} if isinstance(expect, str) else expect):
            raise InvalidTransition(f"{item_id} is {frm}, not {expect}")
        if to not in ALLOWED[frm]:
            raise InvalidTransition(f"{item_id}: {frm} → {to} not allowed")
        ts = now()
        sets = ["state = ?", "state_since = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields]
        db.run(f"UPDATE items SET {', '.join(sets)} WHERE id = ?",
               to, ts, ts, *fields.values(), item_id)
        event(db, item_id, "state", actor, {"from": frm, "to": to, **(data or {})})
        return item(db, item_id)


def update(db: DB, item_id: str, **fields: Any) -> None:
    """Set ITEM_FIELDS without a state change."""
    bad = set(fields) - ITEM_FIELDS
    if bad:
        raise ValueError(f"unknown item fields {sorted(bad)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.run(f"UPDATE items SET {sets}, updated_at = ? WHERE id = ?", *fields.values(), now(),
           item_id)


def failed_from(db: DB, item_id: str) -> str | None:
    """The state an item was in when it last moved to `failed` (what Retry returns to)."""
    for r in db.all("SELECT data_json FROM events WHERE item_id = ? AND kind = 'state' "
                    "ORDER BY id DESC", item_id):
        d = json.loads(r["data_json"])
        if d.get("to") == "failed":
            return d["from"]
    return None


def unmet_dependencies(db: DB, it: sqlite3.Row, need: str) -> list[str]:
    """Dependencies not yet at `need`: 'approved' (to plan) or 'merged' (to implement)."""
    ok = {"approved", "implementing", "ready_to_test", "changes_requested", "merging",
          "merged"} if need == "approved" else {"merged"}
    out = []
    for d in depends(it):
        row = db.one("SELECT state FROM items WHERE id = ?", d)
        if row is None or row["state"] not in ok:
            out.append(d)
    return out


def inbox(db: DB, project_id: int | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT items.*, projects.slug AS project_slug, projects.name AS project_name "
           "FROM items JOIN projects ON projects.id = items.project_id "
           f"WHERE state IN ({', '.join('?' * len(INBOX))})")
    args: list[Any] = list(INBOX)
    if project_id is not None:
        sql += " AND project_id = ?"
        args.append(project_id)
    return db.all(sql + " ORDER BY state_since", *args)


def events(db: DB, item_id: str) -> list[sqlite3.Row]:
    return db.all("SELECT * FROM events WHERE item_id = ? ORDER BY id", item_id)
