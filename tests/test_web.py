"""Routes through FastAPI's TestClient, with real runs against fake_claude."""

import json
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
    r = post(client, "/p/TOY/roadmap", text="## TOY-001: Greet many\n- kind: Two Words\n")
    assert r.status_code == 400 and "line 2" in r.text
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


def test_github_project_gets_a_deploy_key(client, db, cfg, monkeypatch):
    r = client.post("/projects/new", data={"slug": "GH", "name": "Gh", "base_branch": "main",
                                           "origin": "https://github.com/someone/thing"},
                    follow_redirects=False)
    assert r.headers["location"] == "/p/GH/settings#deploy-key"
    assert it.project(db, "GH")["origin"] == "git@github.com:someone/thing.git"
    key = cfg.data_dir / "keys" / "gh_ed25519"
    assert key.stat().st_mode & 0o777 == 0o600
    page = client.get("/p/GH/settings").text
    assert (key.with_suffix(".pub").read_text().split()[1]) in page
    assert "github.com/someone/thing/settings/keys/new" in page
    assert "reach the repo yet" in client.get("/p/GH").text

    seen = {}

    def fake_check(p):
        seen["cmd"] = p.ssh_command
        return True, "read and push access work"
    monkeypatch.setattr("pace.git.check_access", fake_check)
    post(client, "/p/GH/check-access")
    assert str(key) in seen["cmd"]
    assert "✓ read and push access work" in client.get("/p/GH/settings").text
    assert "reach the repo yet" not in client.get("/p/GH").text


def test_alias_and_local_origins_get_no_key(client, db, cfg, project):
    for origin in ("git@github-nkms:a/b.git", project["origin"]):
        client.post("/projects/new", data={"slug": "AL" if "github" in origin else "LO",
                                           "name": "x", "base_branch": "main", "origin": origin})
    assert not (cfg.data_dir / "keys").exists()
    assert it.project(db, "AL")["origin"] == "git@github-nkms:a/b.git"
    assert "Deploy key" not in client.get("/p/AL/settings").text


def test_import_through_ui(client, db, runner, scenario, project):
    md = "# v0.4\nRelease.\n\n## TOY-001: Export\n- kind: debt\n\n## TOY-002: Docs\n- depends: TOY-001\n"
    scenario("plan_ready " + json.dumps({"roadmap_markdown": md, "notes": "Read **ROADMAP.md**."}))
    page = client.get("/p/TOY").text
    assert "Start import" in page and "<details class=\"panel import-box\" open>" in page
    r = post(client, "/p/TOY/import", runner, instructions="docs/roadmap only")
    assert r.headers["location"] == "/i/TOY-IMPORT-1"
    page = client.get("/i/TOY-IMPORT-1").text
    assert "Apply to roadmap" in page and "<strong>ROADMAP.md</strong>" in page
    assert "2 items in 1 group" in page and "Export" in page
    assert "Roadmap import" in client.get("/p/TOY").text  # banner while it's open

    post(client, "/i/TOY-IMPORT-1/approve", runner)
    page = client.get("/p/TOY").text
    assert "v0.4" in page and "TOY-001" in page and "badge kind\">debt" in page
    assert "Roadmap import" not in page

    gid = it.groups(db, project["id"])[0]["id"]
    post(client, "/i/TOY-002/edit", title="Docs", description="", depends="TOY-001", kind="chore",
         group_id="")
    row = it.item(db, "TOY-002")
    assert (row["kind"], row["group_id"]) == ("chore", None)
    post(client, "/p/TOY/items", title="Late", group_id=str(gid))
    assert it.item(db, "TOY-003")["group_id"] == gid
