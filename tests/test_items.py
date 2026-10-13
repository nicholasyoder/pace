import json

import pytest

from pace import items as it
from pace.db import DB
from pace.roadmap import RoadmapError


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "pace.db")


@pytest.fixture
def proj(db):
    return it.create_project(db, slug="WEB", name="Web", origin="/tmp/x.git")


def titles(db, proj):
    return [(r["id"], r["title"]) for r in it.items(db, proj["id"], active_only=True)]


def test_project_slug_validated(db):
    with pytest.raises(ValueError):
        it.create_project(db, slug="web", name="w", origin="o")


def test_add_and_move(db, proj):
    a = it.add_item(db, proj["id"], "A")
    b = it.add_item(db, proj["id"], "B")
    c = it.add_item(db, proj["id"], "C", top=True)
    assert (a, b, c) == ("WEB-001", "WEB-002", "WEB-003")
    assert [i for i, _ in titles(db, proj)] == [c, a, b]
    it.move(db, b, "top")
    it.move(db, c, "down")
    assert [i for i, _ in titles(db, proj)] == [b, a, c]
    it.move(db, c, "down")  # already last
    assert [i for i, _ in titles(db, proj)] == [b, a, c]


def test_depends_validated(db, proj):
    a = it.add_item(db, proj["id"], "A")
    with pytest.raises(ValueError, match="unknown dependency"):
        it.add_item(db, proj["id"], "B", depends=["WEB-099"])
    with pytest.raises(ValueError, match="itself"):
        it.edit_item(db, a, "A", "", [a])


def test_transitions_and_events(db, proj):
    a = it.add_item(db, proj["id"], "A")
    it.transition(db, a, "planning", "system")
    with pytest.raises(it.InvalidTransition):
        it.transition(db, a, "merged", "human")
    with pytest.raises(it.InvalidTransition, match="not plan_ready"):
        it.transition(db, a, "approved", "human", expect="plan_ready")
    row = it.transition(db, a, "failed", "system", last_error="boom")
    assert row["state"] == "failed" and row["last_error"] == "boom"
    assert it.failed_from(db, a) == "planning"
    kinds = [(e["kind"], json.loads(e["data_json"]).get("to")) for e in it.events(db, a)]
    assert kinds == [("created", None), ("state", "planning"), ("state", "failed")]


def test_attempts_reset_rules(db, proj):
    a = it.add_item(db, proj["id"], "A")
    it.transition(db, a, "planning", "system")
    it.update(db, a, attempts=2)
    assert it.transition(db, a, "failed", "system")["attempts"] == 2  # failing keeps it
    assert it.transition(db, a, "planning", "system")["attempts"] == 2  # re-queue keeps it
    it.transition(db, a, "failed", "system")
    assert it.transition(db, a, "planning", "human")["attempts"] == 0  # Retry resets
    it.update(db, a, attempts=1)
    assert it.transition(db, a, "plan_ready", "claude")["attempts"] == 0  # moved on


def test_transition_rolls_back_with_outer_tx(db, proj):
    a = it.add_item(db, proj["id"], "A")
    with pytest.raises(RuntimeError):
        with db.tx():
            it.transition(db, a, "planning", "system")
            raise RuntimeError
    assert it.item(db, a)["state"] == "backlog"
    assert len(it.events(db, a)) == 1


def test_markdown_save(db, proj):
    a = it.add_item(db, proj["id"], "A")
    b = it.add_item(db, proj["id"], "B")
    md = it.active_markdown(db, proj["id"])
    assert md == f"## {a}: A\n\n## {b}: B\n"

    created = it.save_markdown(db, proj["id"],
                               f"## New one\n\n## {b}: B renamed\n- depends: {a}\n\n## {a}: A\n")
    assert created == ["WEB-003"]
    assert titles(db, proj) == [("WEB-003", "New one"), (b, "B renamed"), (a, "A")]
    assert it.depends(it.item(db, b)) == [a]


def test_markdown_save_missing_and_errors(db, proj):
    a = it.add_item(db, proj["id"], "A")
    b = it.add_item(db, proj["id"], "B")
    with pytest.raises(it.MissingItems) as ei:
        it.save_markdown(db, proj["id"], f"## {a}: A\n")
    assert ei.value.ids == [b]
    it.save_markdown(db, proj["id"], f"## {a}: A\n", park_missing=True)
    assert it.item(db, b)["state"] == "parked"

    with pytest.raises(RoadmapError, match="WEB-002 is parked"):
        it.save_markdown(db, proj["id"], f"## {a}: A\n\n## {b}: B\n")
    with pytest.raises(RoadmapError, match="WEB-050 is unknown"):
        it.save_markdown(db, proj["id"], f"## {a}: A\n\n## WEB-050: x\n")
    with pytest.raises(RoadmapError, match="unknown dependency WEB-077"):
        it.save_markdown(db, proj["id"], f"## {a}: A\n- depends: WEB-077\n")

    it.transition(db, a, "planning", "system")
    with pytest.raises(RoadmapError, match="in progress"):
        it.save_markdown(db, proj["id"], "## other\n", park_missing=True)
    assert titles(db, proj) == [(a, "A")]  # nothing saved


def test_inbox_and_deps(db, proj):
    a = it.add_item(db, proj["id"], "A")
    b = it.add_item(db, proj["id"], "B", depends=[a])
    assert it.unmet_dependencies(db, it.item(db, b), "approved") == [a]
    it.transition(db, a, "planning", "system")
    it.transition(db, a, "plan_ready", "claude")
    assert [r["id"] for r in it.inbox(db)] == [a]
    it.transition(db, a, "approved", "human")
    assert it.unmet_dependencies(db, it.item(db, b), "approved") == []
    assert it.unmet_dependencies(db, it.item(db, b), "merged") == [a]
    assert it.inbox(db) == []


def test_groups_order_and_move(db, proj):
    md = ("Be careful.\n\n## Loose\n\n# Tier 1\nFoundations.\n\n## A\n\n## B\n- kind: debt\n\n"
          "# Tier 2\n\n## C\n")
    it.save_markdown(db, proj["id"], md)
    assert it.project(db, proj["id"])["preamble"] == "Be careful."
    rows = it.items(db, proj["id"])
    assert [(r["title"], r["group_title"], r["kind"]) for r in rows] == [
        ("Loose", None, ""), ("A", "Tier 1", ""), ("B", "Tier 1", "debt"), ("C", "Tier 2", "")]
    b = rows[2]["id"]
    it.move(db, b, "top")  # top of its group, not of the roadmap
    assert [r["title"] for r in it.items(db, proj["id"])] == ["Loose", "B", "A", "C"]
    assert it.active_markdown(db, proj["id"]).startswith("Be careful.\n\n## WEB-001: Loose\n")

    # reorder groups, drop the empty one, move an item across groups
    t2 = next(g["id"] for g in it.groups(db, proj["id"]) if g["title"] == "Tier 2")
    it.edit_item(db, b, "B", "", [], group_id=t2)
    assert [r["title"] for r in it.items(db, proj["id"])] == ["Loose", "A", "C", "B"]
    md = it.active_markdown(db, proj["id"]).replace("# Tier 1\nFoundations.\n\n## WEB-002: A\n", "")
    it.save_markdown(db, proj["id"], md.replace("## WEB-001: Loose", "## WEB-002: A\n\n## WEB-001: Loose"))
    assert [g["title"] for g in it.groups(db, proj["id"])] == ["Tier 2"]


def test_parked_in_markdown(db, proj):
    a = it.add_item(db, proj["id"], "A")
    it.save_markdown(db, proj["id"], f"## {a}: A\n- parked\n\n## New\n- parked\n")
    assert [r["state"] for r in it.items(db, proj["id"])] == ["parked", "parked"]
    assert it.active_markdown(db, proj["id"]) == ""


def test_import_check_and_apply(db, proj):
    existing = it.add_item(db, proj["id"], "Existing")
    imp = it.add_import(db, proj["id"], "use docs/roadmap")
    assert imp == "WEB-IMPORT-1" and it.items(db, proj["id"]) [0]["id"] == existing
    bad = "## WEB-001: clash\n\n## WEB-002: x\n- depends: WEB-077\n"
    with pytest.raises(RoadmapError) as ei:
        it.check_import(db, proj["id"], bad)
    assert "already exists" in str(ei.value) and "unknown dependency WEB-077" in str(ei.value)

    md = ("Framing.\n\n# v0.4\nRelease notes.\n\n## WEB-002: First\n- depends: WEB-001\n\n"
          "## WEB-003: Second\n- depends: WEB-002\n- kind: debt\n\n# Someday\n\n## WEB-004: Net\n- parked\n")
    it.add_item(db, proj["id"], "Sneaks in and takes WEB-002")
    created = it.apply_import(db, proj["id"], md, imp)
    assert created == ["WEB-003", "WEB-004", "WEB-005"]  # fresh IDs, deps remapped
    assert it.depends(it.item(db, "WEB-003")) == ["WEB-001"]
    assert it.depends(it.item(db, "WEB-004")) == ["WEB-003"]
    assert it.item(db, "WEB-005")["state"] == "parked"
    assert it.project(db, proj["id"])["preamble"] == "Framing."
    assert [r["title"] for r in it.items(db, proj["id"], active_only=True)] == [
        "Existing", "Sneaks in and takes WEB-002", "First", "Second"]


def propose(db, proj, title, source, reason="because", description="Do it."):
    return db.run("INSERT INTO proposals (project_id, source_item_id, title, description, reason) "
                  "VALUES (?, ?, ?, ?, ?)", proj["id"], source, title, description,
                  reason).lastrowid


def test_default_group(db, proj):
    assert it.default_group(db, proj["id"]) is None
    db.run("INSERT INTO groups (project_id, position, title) VALUES (?, 0, 'Now'), (?, 1, 'Later')",
           proj["id"], proj["id"])
    now_, later = [g["id"] for g in it.groups(db, proj["id"])]
    assert it.default_group(db, proj["id"]) == later  # no active items: the last group
    a = it.add_item(db, proj["id"], "A", group_id=now_)
    assert it.default_group(db, proj["id"]) == now_  # the last group with active items
    it.transition(db, a, "parked", "human")
    assert it.default_group(db, proj["id"]) == later


def test_accept_and_dismiss_proposals(db, proj):
    db.run("INSERT INTO groups (project_id, position, title) VALUES (?, 0, 'Now'), (?, 1, 'Later')",
           proj["id"], proj["id"])
    now_, later = [g["id"] for g in it.groups(db, proj["id"])]
    src = it.add_item(db, proj["id"], "Source", group_id=now_)
    first = it.add_item(db, proj["id"], "Already later", group_id=later)
    a = propose(db, proj, "Split off docs", src)
    b = propose(db, proj, "Drop the cache", src, reason="")
    assert [p["id"] for p in it.open_proposals(db)] == [a, b]

    new = it.accept_proposal(db, a, later)
    row = it.item(db, new)
    assert (row["title"], row["group_id"], row["state"]) == ("Split off docs", later, "backlog")
    assert row["description"] == f"Do it.\n\nProposed by {src}: because"
    assert [r["id"] for r in it.items(db, proj["id"]) if r["group_id"] == later] == [first, new]
    assert "proposal_accepted" in [e["kind"] for e in it.events(db, new)]
    p = db.one("SELECT * FROM proposals WHERE id = ?", a)
    assert (p["status"], p["item_id"]) == ("accepted", new) and p["decided_at"]
    with pytest.raises(ValueError, match="already accepted"):
        it.accept_proposal(db, a, later)
    with pytest.raises(ValueError, match="already accepted"):
        it.dismiss_proposal(db, a)

    it.dismiss_proposal(db, b)
    assert db.one("SELECT status FROM proposals WHERE id = ?", b)[0] == "dismissed"
    assert it.open_proposals(db) == []
    with pytest.raises(ValueError, match="already dismissed"):
        it.accept_proposal(db, b, None)
    with pytest.raises(ValueError, match="unknown group"):  # nothing half-done
        it.accept_proposal(db, propose(db, proj, "Third", src), 999)
    assert [p["title"] for p in it.open_proposals(db)] == ["Third"]
