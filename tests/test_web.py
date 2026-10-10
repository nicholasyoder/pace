"""Routes through FastAPI's TestClient, with real runs against fake_claude."""

import re

import pytest
from fastapi.testclient import TestClient

from pace import items as it
from pace.main import create_app


@pytest.fixture
def client(cfg, runner):
    with TestClient(create_app(cfg, runner)) as c:
        yield c


def post(client, url, runner=None, **data):
    r = client.post(url, data=data, follow_redirects=False)
    if runner:
        runner.wait()
    return r


@pytest.fixture
def item(db, project):
    return it.add_item(db, project["id"], "Greet many", "Support <b>greeting</b> many.")


def test_create_project_and_quick_add(client, db, origin):
    r = client.post("/projects/new", data={"slug": "WEB", "name": "Web", "origin": str(origin),
                                           "base_branch": "main", "merge_mode": "local",
                                           "merge_strategy": "squash", "plan_ahead": "3",
                                           "test_ahead": "2",
                                           "max_concurrent_implementations": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/p/WEB"
    assert client.post("/projects/new", data={"slug": "WEB", "name": "x", "origin": "o",
                                              "base_branch": "main"}).status_code == 400
    assert post(client, "/p/WEB/items", title="First thing").status_code == 303
    page = client.get("/p/WEB").text
    assert "WEB-001" in page and "First thing" in page


def test_full_flow_through_ui(client, db, runner, scenario, project, item):
    scenario("plan_needs_input", "plan_ready", "implement_done")
    page = client.get(f"/i/{item}").text
    assert "&lt;b&gt;greeting&lt;/b&gt;" in page  # Claude/user text is escaped

    post(client, f"/i/{item}/plan", runner)
    page = client.get(f"/i/{item}").text
    assert "needs input" in page and 'name="q_q1_format"' in page
    assert client.get("/").text.count(f"/i/{item}") == 1  # in the inbox

    # nothing chosen and not delegated → error shown, state unchanged
    r = post(client, f"/i/{item}/answer")
    assert r.status_code == 409 and "has no answer" in r.text
    post(client, f"/i/{item}/answer", runner, decide_rest="1")
    assert it.item(db, item)["state"] == "plan_ready"
    answers = db.one("SELECT answers_json FROM qa_rounds")["answers_json"]
    assert "recommended option" in answers

    post(client, f"/i/{item}/approve", runner)
    page = client.get(f"/i/{item}").text
    assert "Works → merge" in page and "git checkout pace/TOY-001" in page
    assert "fake_change.txt" in page  # diff

    post(client, f"/i/{item}/merge", runner)
    assert it.item(db, item)["state"] == "merged"
    run_id = db.one("SELECT id FROM runs ORDER BY id LIMIT 1")["id"]
    assert "result: success" in client.get(f"/runs/{run_id}").text


def test_double_approve_is_refused(client, db, runner, scenario, item):
    scenario("plan_ready", "implement_done")
    post(client, f"/i/{item}/plan", runner)
    post(client, f"/i/{item}/approve", runner)
    r = post(client, f"/i/{item}/approve", runner)
    assert r.status_code == 409 and "not plan_ready" in r.text


def test_roadmap_markdown_edit(client, db, project, item):
    page = client.get("/p/TOY/roadmap").text
    assert "## TOY-001: Greet many" in page
    r = post(client, "/p/TOY/roadmap", text="junk\n## TOY-001: Greet many\n")
    assert r.status_code == 400 and "line 1" in r.text
    r = post(client, "/p/TOY/roadmap", text="## Something else\n")
    assert r.status_code == 409 and "leaves out TOY-001" in r.text
    r = post(client, "/p/TOY/roadmap", text="## Something else\n", park_missing="1")
    assert r.status_code == 303
    assert it.item(db, item)["state"] == "parked"
    assert [x["title"] for x in it.items(db, project["id"], active_only=True)] == ["Something else"]


def test_move_and_settings(client, db, project, item):
    b = it.add_item(db, project["id"], "Second")
    post(client, f"/i/{b}/move", where="top", next="/p/TOY")
    assert [x["id"] for x in it.items(db, project["id"])] == [b, item]
    r = post(client, "/p/TOY/settings", name="Toy2", origin=project["origin"], base_branch="main",
             merge_mode="local", merge_strategy="merge", plan_ahead="0", test_ahead="2",
             max_concurrent_implementations="1", model="", extra_instructions="Be terse.")
    assert r.status_code == 303
    p = it.project(db, "TOY")
    assert (p["name"], p["merge_strategy"], p["plan_ahead"], p["extra_instructions"]) == \
        ("Toy2", "merge", 0, "Be terse.")
    assert 'value="0"' in client.get("/p/TOY/settings").text


def test_edit_item(client, db, project, item):
    r = post(client, f"/i/{item}/edit", title="Renamed", description="New", depends="TOY-099")
    assert r.status_code == 400 and "unknown dependency" in r.text
    post(client, f"/i/{item}/edit", title="Renamed", description="New", depends="")
    assert it.item(db, item)["title"] == "Renamed"


def test_pages_render(client, db, runner, scenario, project, item):
    scenario('plan_ready {"status": "blocked", "blocked_reason": "can\'t"}')
    post(client, f"/i/{item}/plan", runner)
    page = client.get(f"/i/{item}").text
    assert "Retry" in page and "Claude is blocked" in page
    assert re.search(r"<title>\(1\) ", client.get("/").text)  # inbox count in title
    for url in ("/", "/?project=TOY", "/p/TOY?show=all", "/projects/new", "/p/TOY/settings"):
        assert client.get(url).status_code == 200, url
    assert client.get("/i/NOPE-001").status_code == 404
