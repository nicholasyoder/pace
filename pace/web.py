"""Web UI routes (§13): server-rendered pages, form POSTs, SSE for live updates.

Every POST redirects back (303) on success. An ActionError or validation
problem re-renders the page with the message instead.
"""

from __future__ import annotations

import asyncio
import difflib
import gzip
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from pace import actions as act
from pace import git as g
from pace import items as it
from pace.db import now
from pace.roadmap import RoadmapError
from pace.runs import activity_line, git_project, latest_impl_output, latest_plan

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


# --- template helpers -------------------------------------------------------------

def md(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))  # html=False: raw HTML in Claude's text is escaped


def md_inline(text: str | None) -> Markup:
    return Markup(_md.renderInline(text or ""))


def diff_html(text: str) -> Markup:
    out = []
    for line in text.splitlines():
        cls = ("add" if line.startswith("+") and not line.startswith("+++") else
               "del" if line.startswith("-") and not line.startswith("---") else
               "hunk" if line.startswith("@@") else
               "meta" if line.startswith(("diff ", "index ", "+++", "---")) else "")
        out.append(f'<span class="{cls}">{escape(line)}</span>' if cls else str(escape(line)))
    return Markup("\n".join(out))


def age(ts: str | None) -> str:
    if not ts:
        return ""
    secs = int((datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit}"
    return "<1m"


def fromjson(s: str | None) -> Any:
    return json.loads(s) if s else None


templates.env.filters.update(md=md, mdi=md_inline, diff=diff_html, age=age, fromjson=fromjson)

INBOX_GROUPS = [("needs_input", "Questions"), ("plan_ready", "Plans to approve"),
                ("ready_to_test", "Ready to test"), ("failed", "Problems")]


def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    st = request.app.state
    ctx.setdefault("error", None)
    return templates.TemplateResponse(request, name, {
        "inbox_count": len(it.inbox(st.db)),
        "all_projects": it.projects(st.db),
        "rate_limit": st.db.kv_get("rate_limit"),
        "running": st.runner.active(),
        **ctx,
    }, status_code=status)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def db_(request: Request):
    return request.app.state.db


def get_item(request: Request, item_id: str):
    try:
        return it.item(db_(request), item_id)
    except it.NotFound:
        raise HTTPException(404, f"no item {item_id}")


def get_project(request: Request, slug: str):
    try:
        return it.project(db_(request), slug)
    except it.NotFound:
        raise HTTPException(404, f"no project {slug}")


# --- inbox ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def inbox(request: Request, project: str | None = None):
    db = db_(request)
    pid = get_project(request, project)["id"] if project else None
    rows = it.inbox(db, pid)
    groups = [(label, [r for r in rows if r["state"] == s]) for s, label in INBOX_GROUPS]
    busy = db.all("SELECT items.*, projects.slug AS project_slug FROM items JOIN projects ON "
                  "projects.id = project_id WHERE state IN ('planning', 'implementing', "
                  "'merging') ORDER BY state_since")
    return render(request, "inbox.html", groups=groups, busy=busy, project_filter=project)


# --- projects --------------------------------------------------------------------------

PROJECT_INTS = ("plan_ahead", "test_ahead", "max_concurrent_implementations")


def project_form(form) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for k in ("name", "origin", "base_branch", "merge_mode", "merge_strategy", "model",
              "extra_instructions"):
        if k in form:
            fields[k] = str(form[k]).strip()
    for k in PROJECT_INTS:
        if k in form:
            fields[k] = int(form[k])
    for k in ("name", "origin", "base_branch"):
        if k in fields and not fields[k]:
            raise ValueError(f"{k} is required")
    if "origin" in fields:
        fields["origin"] = g.normalize_origin(fields["origin"])
    return fields


def deploy_key(request: Request, p) -> dict[str, Any] | None:
    """Make sure a github.com project has its deploy key; return what the UI shows."""
    gh = g.github_repo(p["origin"])
    if gh is None:
        return None
    cfg = request.app.state.cfg
    pub = g.ensure_deploy_key(g.key_path(cfg.data_dir, p["slug"]), f"pace-{p['slug'].lower()}")
    return {"public": pub, "owner": gh[0], "repo": gh[1],
            "add_url": f"https://github.com/{gh[0]}/{gh[1]}/settings/keys/new",
            "access": db_(request).kv_get(f"access:{p['slug']}")}


def settings_page(request: Request, p, status: int = 200, values=None, error=None):
    return render(request, "project_settings.html", status, project=p,
                  values=values if values is not None else dict(p), error=error,
                  key=deploy_key(request, p))


@router.get("/projects/new", response_class=HTMLResponse)
def project_new(request: Request):
    return render(request, "project_settings.html", project=None, values={})


@router.post("/projects/new")
async def project_create(request: Request):
    form = await request.form()
    try:
        fields = project_form(form)
        p = it.create_project(db_(request), slug=str(form.get("slug", "")).strip(), **fields)
    except (ValueError, sqlite3.IntegrityError) as e:
        msg = "that slug is taken" if isinstance(e, sqlite3.IntegrityError) else str(e)
        return render(request, "project_settings.html", 400, project=None, values=dict(form),
                      error=msg)
    if deploy_key(request, p):
        return back(f"/p/{p['slug']}/settings#deploy-key")
    return back(f"/p/{p['slug']}")


@router.get("/p/{slug}", response_class=HTMLResponse)
def project_page(request: Request, slug: str, show: str = "active"):
    p = get_project(request, slug)
    db = db_(request)
    rows = it.items(db, p["id"], active_only=(show == "active"))
    unmet = {r["id"]: it.unmet_dependencies(db, r, "approved" if r["state"] == "backlog"
                                            else "merged")
             for r in rows if r["state"] in ("backlog", "approved")}
    key = deploy_key(request, p)
    return render(request, "project.html", project=p, rows=rows, show=show, unmet=unmet,
                  needs_key=bool(key and not (key["access"] or {}).get("ok")))


@router.post("/p/{slug}/items")
async def quick_add(request: Request, slug: str):
    p = get_project(request, slug)
    form = await request.form()
    try:
        it.add_item(db_(request), p["id"], str(form.get("title", "")),
                    str(form.get("description", "")), top=bool(form.get("top")))
    except ValueError as e:
        return render(request, "project.html", 400, project=p, show="active", unmet={},
                      rows=it.items(db_(request), p["id"], active_only=True), error=str(e))
    request.app.state.runner.publish("-")
    return back(f"/p/{slug}")


@router.get("/p/{slug}/settings", response_class=HTMLResponse)
def project_settings(request: Request, slug: str):
    return settings_page(request, get_project(request, slug))


@router.post("/p/{slug}/settings")
async def project_save(request: Request, slug: str):
    p = get_project(request, slug)
    form = await request.form()
    try:
        fields = project_form(form)
        it.update_project(db_(request), p["id"], **fields, paused=int(bool(form.get("paused"))))
    except ValueError as e:
        return settings_page(request, p, 400, values=dict(form), error=str(e))
    if fields.get("origin") != p["origin"] or fields.get("base_branch") != p["base_branch"]:
        db_(request).run("DELETE FROM kv WHERE key = ?", f"access:{slug}")  # re-check
    p = get_project(request, slug)
    if deploy_key(request, p) and not (db_(request).kv_get(f"access:{slug}") or {}).get("ok"):
        return back(f"/p/{slug}/settings#deploy-key")
    return back(f"/p/{slug}")


@router.post("/p/{slug}/check-access")
async def project_check_access(request: Request, slug: str):
    p = get_project(request, slug)
    P = git_project(request.app.state.cfg, p)
    try:
        ok, message = await asyncio.to_thread(g.check_access, P)
    except Exception as e:  # e.g. a hung ssh hitting the timeout
        ok, message = False, f"{type(e).__name__}: {e}"
    db_(request).kv_set(f"access:{slug}", {"ok": ok, "message": message, "at": now()})
    return back(f"/p/{slug}/settings#deploy-key")


@router.get("/p/{slug}/roadmap", response_class=HTMLResponse)
def roadmap_edit(request: Request, slug: str):
    p = get_project(request, slug)
    return render(request, "roadmap.html", project=p,
                  text=it.active_markdown(db_(request), p["id"]), errors=[], missing=[])


@router.post("/p/{slug}/roadmap")
async def roadmap_save(request: Request, slug: str):
    p = get_project(request, slug)
    form = await request.form()
    text = str(form.get("text", ""))
    try:
        it.save_markdown(db_(request), p["id"], text, park_missing=bool(form.get("park_missing")))
    except RoadmapError as e:
        return render(request, "roadmap.html", 400, project=p, text=text, errors=e.errors,
                      missing=[])
    except it.MissingItems as e:
        return render(request, "roadmap.html", 409, project=p, text=text, errors=[],
                      missing=e.ids)
    request.app.state.runner.publish("-")
    return back(f"/p/{slug}")


# --- items -------------------------------------------------------------------------------

def item_context(request: Request, item_id: str) -> dict[str, Any]:
    st = request.app.state
    db, cfg = st.db, st.cfg
    item = get_item(request, item_id)
    proj = it.project(db, item["project_id"])
    plans = db.all("SELECT * FROM plans WHERE item_id = ? ORDER BY version DESC", item_id)
    plan_diff = ""
    if len(plans) > 1:
        plan_diff = "\n".join(difflib.unified_diff(
            plans[1]["markdown"].splitlines(), plans[0]["markdown"].splitlines(),
            f"v{plans[1]['version']}", f"v{plans[0]['version']}", lineterm=""))
    rounds = db.all("SELECT * FROM qa_rounds WHERE item_id = ? ORDER BY round", item_id)
    open_round = rounds[-1] if rounds and rounds[-1]["answers_json"] is None \
        and item["state"] == "needs_input" else None
    impl = latest_impl_output(db, item_id)
    P = git_project(cfg, proj)
    diff = diffstat = log = ""
    if item["state"] in ("ready_to_test", "changes_requested", "merging") and P.canonical.exists():
        diff, diffstat, log = (g.branch_diff(P, item_id), g.branch_diff(P, item_id, stat=True),
                               g.branch_log(P, item_id))
    runs = db.all("SELECT * FROM runs WHERE item_id = ? ORDER BY id DESC", item_id)
    current = next((r for r in runs if r["status"] in ("queued", "running")), None)
    deps = [(d, db.one("SELECT state, title FROM items WHERE id = ?", d)) for d in it.depends(item)]
    need = "approved" if item["state"] == "backlog" else "merged"
    events = list(reversed(it.events(db, item_id)))
    return dict(item=item, project=proj, plans=plans, plan_diff=plan_diff, rounds=rounds,
                open_round=open_round, impl=impl, diff=diff, diffstat=diffstat, log=log,
                runs=runs, current=current, deps=deps, events=events,
                live_lines=run_lines(current) if current else [],
                unmet=it.unmet_dependencies(db, item, need) if item["state"] in
                ("backlog", "approved") else [],
                busy=st.runner.busy(item_id), branch=g.branch_name(item_id),
                total_cost=sum(r["cost_usd"] or 0 for r in runs),
                proposals=db.all("SELECT * FROM proposals WHERE source_item_id = ?", item_id))


@router.get("/i/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: str):
    return render(request, "item.html", **item_context(request, item_id))


@router.post("/i/{item_id}/edit")
async def item_edit(request: Request, item_id: str):
    get_item(request, item_id)
    form = await request.form()
    deps = [d.strip() for d in str(form.get("depends", "")).replace(",", " ").split() if d.strip()]
    try:
        it.edit_item(db_(request), item_id, str(form.get("title", "")),
                     str(form.get("description", "")), deps)
    except ValueError as e:
        return render(request, "item.html", 400, error=str(e), **item_context(request, item_id))
    return back(f"/i/{item_id}")


def answers_from_form(form, questions: list[dict]) -> dict[str, str]:
    out = {}
    for q in questions:
        opt = str(form.get(f"q_{q['id']}", "")).strip()
        text = str(form.get(f"t_{q['id']}", "")).strip()
        label = next((o["label"] for o in q["options"] if o["id"] == opt), "")
        parts = [f"option {opt} ({label})" if opt else "", text]
        out[q["id"]] = "; ".join(p for p in parts if p)
    return out


ACTIONS = {
    "plan": act.plan, "approve": act.approve, "implement": act.implement,
    "merge": act.merge, "park": act.park, "unpark": act.unpark, "retry": act.retry,
    "backlog": act.to_backlog, "cancel": act.cancel, "address_feedback": act.address_feedback,
}


@router.post("/i/{item_id}/{action}")
async def item_action(request: Request, item_id: str, action: str):
    st = request.app.state
    item = get_item(request, item_id)
    form = await request.form()
    try:
        if action in ACTIONS:
            await asyncio.to_thread(ACTIONS[action], st.runner, item_id)
        elif action == "answer":
            rnd = st.db.one("SELECT questions_json FROM qa_rounds WHERE item_id = ? "
                            "ORDER BY round DESC LIMIT 1", item_id)
            qs = json.loads(rnd["questions_json"]) if rnd else []
            await asyncio.to_thread(act.answer, st.runner, item_id, answers_from_form(form, qs),
                                    bool(form.get("decide_rest")))
        elif action == "revise":
            await asyncio.to_thread(act.revise_plan, st.runner, item_id,
                                    str(form.get("feedback", "")))
        elif action == "problem":
            await asyncio.to_thread(act.problem, st.runner, item_id, str(form.get("note", "")))
        elif action == "move":
            it.move(st.db, item_id, str(form.get("where", "")))
            return back(str(form.get("next") or f"/p/{it.project(st.db, item['project_id'])['slug']}"))
        else:
            raise HTTPException(404, f"no action {action}")
    except (act.ActionError, it.InvalidTransition) as e:
        return render(request, "item.html", 409, error=str(e), **item_context(request, item_id))
    st.runner.publish(item_id)
    return back(str(form.get("next") or f"/i/{item_id}"))


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int):
    r = db_(request).one("SELECT * FROM runs WHERE id = ?", run_id)
    if r is None:
        raise HTTPException(404)
    return render(request, "run.html", run=r, lines=run_lines(r))


def run_lines(run) -> list[str]:
    lines: list[str] = []
    path = Path(run["log_path"] or "")
    for p in (path, path.with_suffix("")):  # .jsonl.gz once done, .jsonl while running
        if p.is_file():
            opener = gzip.open if p.suffix == ".gz" else open
            with opener(p, "rt") as f:
                for raw in f:
                    try:
                        line = activity_line(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
                    if line:
                        lines.append(line)
            break
    return lines


# --- SSE ----------------------------------------------------------------------------------

def sse(request: Request, *topics: str) -> StreamingResponse:
    bus = request.app.state.runner.bus

    async def gen():
        q = bus.open(*topics)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), 15)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
        finally:
            bus.close(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/events/item/{item_id}")
def item_events(request: Request, item_id: str):
    return sse(request, f"item:{item_id}")


@router.get("/events/global")
def global_events(request: Request):
    return sse(request, "global")


@router.get("/favicon.ico")
def favicon():
    return Response(status_code=204)
