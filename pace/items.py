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
    "plan_ready": {"approved", "planning", "parked", "merged"},  # → merged: imports only
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
                  "extra_instructions", "paused", "preamble"]
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
#
# Priority order is (group position, item position); ungrouped items come
# first. Item positions are only compared within a group. Import items
# (type 'import') are never part of the roadmap itself.

_ITEMS = ("SELECT items.*, g.title AS group_title FROM items "
          "LEFT JOIN groups g ON g.id = items.group_id ")
_ORDER = " ORDER BY COALESCE(g.position, -1), items.position, items.created_at"


def item(db: DB, item_id: str) -> sqlite3.Row:
    row = db.one(_ITEMS + "WHERE items.id = ?", item_id)
    if row is None:
        raise NotFound(f"item {item_id}")
    return row


def items(db: DB, project_id: int, active_only: bool = False) -> list[sqlite3.Row]:
    sql = _ITEMS + "WHERE items.project_id = ? AND items.type = 'item'"
    if active_only:
        sql += " AND items.state NOT IN ('merged', 'parked')"
    return db.all(sql + _ORDER, project_id)


def imports(db: DB, project_id: int) -> list[sqlite3.Row]:
    return db.all(_ITEMS + "WHERE items.project_id = ? AND items.type = 'import' "
                  "ORDER BY items.created_at DESC", project_id)


def groups(db: DB, project_id: int) -> list[sqlite3.Row]:
    return db.all("SELECT * FROM groups WHERE project_id = ? ORDER BY position", project_id)


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


def _check_kind(kind: str) -> str:
    kind = kind.strip().lower()
    if kind and not roadmap.KIND_OK.match(kind):
        raise ValueError("kind must be one lowercase word (e.g. debt)")
    return kind


def _check_group(db: DB, project_id: int, group_id: int | None) -> None:
    if group_id is not None and db.one("SELECT 1 FROM groups WHERE id = ? AND project_id = ?",
                                       group_id, project_id) is None:
        raise ValueError(f"unknown group {group_id}")


def _insert(db: DB, item_id: str, project_id: int, position: int, title: str,
            description: str, deps: list[str], kind: str = "", group_id: int | None = None,
            type_: str = "item") -> None:
    ts = now()
    db.run("INSERT INTO items (id, project_id, position, title, description, depends_json, kind, "
           "group_id, type, state_since, created_at, updated_at) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
           item_id, project_id, position, title, description, json.dumps(deps), kind, group_id,
           type_, ts, ts, ts)


def add_item(db: DB, project_id: int, title: str, description: str = "",
             depends: list[str] | None = None, top: bool = False, group_id: int | None = None,
             kind: str = "") -> str:
    title = title.strip()
    if not title:
        raise ValueError("title is required")
    kind = _check_kind(kind)
    with db.tx():
        p = project(db, project_id)
        _check_depends(db, p, depends or [], None)
        _check_group(db, p["id"], group_id)
        item_id = _next_id(db, p)
        agg = "MIN(position) - 1" if top else "MAX(position) + 1"
        pos = db.one(f"SELECT COALESCE({agg}, 0) FROM items WHERE project_id = ?", p["id"])[0]
        _insert(db, item_id, p["id"], pos, title, description.strip(), depends or [], kind,
                group_id)
        event(db, item_id, "created", "human", {"title": title})
    return item_id


def edit_item(db: DB, item_id: str, title: str, description: str, depends: list[str],
              kind: str | None = None, group_id: int | None | str = "keep") -> None:
    title = title.strip()
    if not title:
        raise ValueError("title is required")
    with db.tx():
        it = item(db, item_id)
        _check_depends(db, project(db, it["project_id"]), depends, item_id)
        kind = it["kind"] if kind is None else _check_kind(kind)
        gid = it["group_id"] if group_id == "keep" else group_id
        _check_group(db, it["project_id"], gid)  # type: ignore[arg-type]
        if gid != it["group_id"]:  # joins the end of its new group
            pos = db.one("SELECT COALESCE(MAX(position) + 1, 0) FROM items WHERE project_id = ?",
                         it["project_id"])[0]
            db.run("UPDATE items SET position = ? WHERE id = ?", pos, item_id)
        db.run("UPDATE items SET title = ?, description = ?, depends_json = ?, kind = ?, "
               "group_id = ?, updated_at = ? WHERE id = ?", title, description.strip(),
               json.dumps(depends), kind, gid, now(), item_id)
        event(db, item_id, "edited", "human", {})


def move(db: DB, item_id: str, where: str) -> None:
    """Reorder among the active items of the same group: up | down | top."""
    with db.tx():
        it = item(db, item_id)
        peers = [r for r in items(db, it["project_id"], active_only=True)
                 if r["group_id"] == it["group_id"]]
        order = [r["id"] for r in peers]
        if item_id not in order:
            return
        slots = sorted(r["position"] for r in peers)
        i = order.index(item_id)
        order.pop(i)
        j = {"up": max(i - 1, 0), "down": min(i + 1, len(order)), "top": 0}[where]
        order.insert(j, item_id)
        if len(set(slots)) < len(slots):  # duplicate positions: spread them out
            slots = list(range(slots[0], slots[0] + len(slots)))
        for pos, iid in zip(slots, order):
            db.run("UPDATE items SET position = ? WHERE id = ?", pos, iid)


def _renumber(db: DB, project_id: int, active_order: list[str]) -> None:
    rest = [r["id"] for r in items(db, project_id) if r["id"] not in set(active_order)]
    for pos, iid in enumerate(active_order + rest):
        db.run("UPDATE items SET position = ? WHERE id = ?", pos, iid)


# --- roadmap markdown ---------------------------------------------------------

def active_markdown(db: DB, project_id: int) -> str:
    rows = items(db, project_id, active_only=True)
    used = {r["group_id"] for r in rows}
    shown = [g for g in groups(db, project_id) if g["id"] in used or not db.one(
        "SELECT 1 FROM items WHERE group_id = ?", g["id"])]
    return roadmap.serialize(roadmap.Roadmap(
        preamble=project(db, project_id)["preamble"],
        groups=[roadmap.Group(g["title"], g["description"]) for g in shown],
        entries=[roadmap.Entry(r["title"], r["id"], depends(r), r["description"], kind=r["kind"],
                               group=r["group_title"]) for r in rows]))


class MissingItems(Exception):
    """The save leaves out active items; ask whether to park them."""

    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(", ".join(ids))


def _sync_groups(db: DB, project_id: int, rm: roadmap.Roadmap, replace: bool) -> dict[str, int]:
    """Upsert the roadmap's groups; return title → id.

    replace: the roadmap lists every group (markdown save): its order wins and
    groups it leaves out are deleted once nothing refers to them. Otherwise
    (import) new groups are appended and existing ones keep their place.
    """
    existing = {g["title"]: g for g in groups(db, project_id)}
    base = 0 if replace else len(existing)
    ids = {}
    for i, g in enumerate(rm.groups):
        if g.title in existing:
            row = existing[g.title]
            desc = g.description if replace or not row["description"] else row["description"]
            pos = i if replace else row["position"]
            db.run("UPDATE groups SET position = ?, description = ? WHERE id = ?", pos, desc,
                   row["id"])
            ids[g.title] = row["id"]
        else:
            cur = db.run("INSERT INTO groups (project_id, position, title, description) "
                         "VALUES (?, ?, ?, ?)", project_id, base + i, g.title, g.description)
            ids[g.title] = cur.lastrowid
    if replace:  # groups left out go after the listed ones; _drop_empty_groups removes them
        for title, row in existing.items():
            if title not in ids:
                db.run("UPDATE groups SET position = ? WHERE id = ?",
                       len(rm.groups) + row["position"], row["id"])
    return ids


def _drop_empty_groups(db: DB, project_id: int, keep: set[int]) -> None:
    db.run("DELETE FROM groups WHERE project_id = ? AND id NOT IN (SELECT DISTINCT group_id "
           f"FROM items WHERE group_id IS NOT NULL) AND id NOT IN ({','.join('?' * len(keep))})",
           project_id, *keep)


def save_markdown(db: DB, project_id: int, text: str, park_missing: bool = False) -> list[str]:
    """Apply an edited active roadmap. Returns the IDs of newly created items.

    Raises RoadmapError for parse/validation problems (nothing saved), or
    MissingItems when active items are left out and park_missing is False.
    """
    p = project(db, project_id)
    rm = roadmap.parse(text, p["slug"])
    with db.tx():
        active = {r["id"]: r for r in items(db, project_id, active_only=True)}
        errors = []
        for e in rm.entries:
            if e.id and e.id not in active:
                row = db.one("SELECT state FROM items WHERE id = ? AND project_id = ?", e.id,
                             project_id)
                why = f"is {row['state']}" if row else "is unknown (remove the ID to add a new item)"
                errors.append(roadmap.ParseError(e.line, f"{e.id} {why}"))
            elif e.id and e.parked and active[e.id]["state"] in BUSY:
                errors.append(roadmap.ParseError(e.line, f"{e.id} can't be parked while a run "
                                                         "or merge is in progress"))
            for d in e.depends:
                if db.one("SELECT 1 FROM items WHERE id = ? AND project_id = ?", d,
                          project_id) is None:
                    errors.append(roadmap.ParseError(e.line, f"unknown dependency {d}"))
        if errors:
            raise roadmap.RoadmapError(errors)
        missing = [iid for iid in active if iid not in {e.id for e in rm.entries}]
        busy = [iid for iid in missing if active[iid]["state"] in BUSY]
        if busy:
            raise roadmap.RoadmapError([roadmap.ParseError(
                0, f"{', '.join(busy)} can't be removed while a run or merge is in progress")])
        if missing and not park_missing:
            raise MissingItems(missing)

        db.run("UPDATE projects SET preamble = ? WHERE id = ?", rm.preamble, project_id)
        gids = _sync_groups(db, project_id, rm, replace=True)
        created, order = [], []
        ts = now()
        for e in rm.entries:
            gid = gids.get(e.group) if e.group else None
            if e.id is None:
                e.id = _next_id(db, p)
                _insert(db, e.id, project_id, 0, e.title, e.description, e.depends, e.kind, gid)
                event(db, e.id, "created", "human", {"title": e.title})
                created.append(e.id)
            else:
                old = active[e.id]
                if (old["title"], old["description"], depends(old), old["kind"],
                        old["group_id"]) != (e.title, e.description, e.depends, e.kind, gid):
                    db.run("UPDATE items SET title = ?, description = ?, depends_json = ?, "
                           "kind = ?, group_id = ?, updated_at = ? WHERE id = ?",
                           e.title, e.description, json.dumps(e.depends), e.kind, gid, ts, e.id)
                    event(db, e.id, "edited", "human", {})
            if e.parked:
                transition(db, e.id, "parked", "human", data={"reason": "marked parked"})
            order.append(e.id)
        for iid in missing:
            transition(db, iid, "parked", "human", data={"reason": "removed from roadmap"})
        _renumber(db, project_id, order)
        _drop_empty_groups(db, project_id, set(gids.values()))
    return created


# --- imports --------------------------------------------------------------------

def add_import(db: DB, project_id: int, instructions: str = "") -> str:
    """An import item: its plan is a proposed roadmap, applied when approved."""
    with db.tx():
        p = project(db, project_id)
        n = db.one("SELECT COUNT(*) FROM items WHERE project_id = ? AND type = 'import'",
                   project_id)[0] + 1
        item_id = f"{p['slug']}-IMPORT-{n}"
        _insert(db, item_id, project_id, 0, "Import roadmap", instructions.strip(), [],
                type_="import")
        event(db, item_id, "created", "human", {"title": "Import roadmap"})
    return item_id


def check_import(db: DB, project_id: int, text: str, strict: bool = True) -> roadmap.Roadmap:
    """Validate a proposed import. IDs in it are provisional labels for new items;
    dependencies may point at them (they win) or at existing items.

    strict (Claude's output): provisional IDs must not reuse existing ones, so a
    dependency is never ambiguous when proposed. Off when applying, since items
    added while the proposal waited may have taken those numbers.
    """
    p = project(db, project_id)
    rm = roadmap.parse(text, p["slug"])
    existing = {r["id"] for r in db.all("SELECT id FROM items WHERE project_id = ?", project_id)}
    provisional = {e.id for e in rm.entries if e.id}
    errors = []
    for e in rm.entries:
        if strict and e.id in existing:
            errors.append(roadmap.ParseError(
                e.line, f"{e.id} already exists; imported items are all new, so number them "
                        f"from {roadmap.format_id(p['slug'], p['next_id'])}"))
        for d in e.depends:
            if d not in provisional and d not in existing:
                errors.append(roadmap.ParseError(e.line, f"unknown dependency {d}"))
    if not rm.entries:
        errors.append(roadmap.ParseError(0, "the roadmap has no items"))
    if errors:
        raise roadmap.RoadmapError(sorted(errors, key=lambda x: x.line))
    return rm


def apply_import(db: DB, project_id: int, text: str, source: str) -> list[str]:
    """Add an approved import's items (fresh IDs, dependencies remapped) after the
    existing roadmap. A non-empty preamble replaces the project's."""
    rm = check_import(db, project_id, text, strict=False)
    with db.tx():
        p = project(db, project_id)
        if rm.preamble:
            db.run("UPDATE projects SET preamble = ? WHERE id = ?", rm.preamble, project_id)
        gids = _sync_groups(db, project_id, rm, replace=False)
        ids = {e.id: _next_id(db, p) for e in rm.entries if e.id}
        pos = db.one("SELECT COALESCE(MAX(position) + 1, 0) FROM items WHERE project_id = ?",
                     project_id)[0]
        created = []
        for e in rm.entries:
            iid = ids[e.id] if e.id else _next_id(db, p)
            _insert(db, iid, project_id, pos, e.title, e.description,
                    [ids.get(d, d) for d in e.depends], e.kind,
                    gids[e.group] if e.group else None)
            pos += 1
            event(db, iid, "created", "human", {"title": e.title, "import": source})
            if e.parked:
                transition(db, iid, "parked", "human", data={"reason": "parked on import"})
            created.append(iid)
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

    `attempts` (runs started in the current state, §9 rule 5) resets whenever
    the item moves on, except into `failed` and when the system moves it out
    of `failed` (re-queueing), so repeated failures add up. A human moving it
    out of `failed` (Retry, back to backlog, park) resets it.
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
        if "attempts" not in fields and to != "failed" and not (frm == "failed"
                                                                and actor != "human"):
            fields["attempts"] = 0
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
