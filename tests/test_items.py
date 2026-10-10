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
