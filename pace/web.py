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
from datetime import UTC, datetime, timedelta
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
from pace import roadmap
from pace import windows
from pace.roadmap import RoadmapError
from pace.runs import activity_line, git_project, latest_impl_output, latest_plan
from pace.scheduler import paused_all, set_paused_all

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


def clock(t: datetime | None) -> str:
    """A window boundary: `22:00`, or `Tue 22:00` when it isn't today."""
    if t is None:
        return "?"
    return t.strftime("%H:%M" if t.date() == windows.local_now().date() else "%a %H:%M")


def until(ts: float) -> str:
    """An epoch time ahead: `Sat 14:30 UTC (in 2h 5m)`."""
    t = datetime.fromtimestamp(ts).astimezone()
    secs = int(ts - datetime.now(UTC).timestamp())
    left = []
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n and len(left) < 2:
            left.append(f"{secs // n}{unit}")
            secs %= n
    return f"{t.strftime('%a %H:%M %Z')} (in {' '.join(left) or '<1m'})"


templates.env.filters.update(md=md, mdi=md_inline, diff=diff_html, age=age, fromjson=fromjson,
                             clock=clock, until=until)

INBOX_GROUPS = [("needs_input", "Questions"), ("plan_ready", "Plans to approve"),
                ("ready_to_test", "Ready to test"), ("failed", "Problems")]


def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    st = request.app.state
    ctx.setdefault("error", None)
    return templates.TemplateResponse(request, name, {
        "inbox_count": len(it.inbox(st.db)) + len(it.open_proposals(st.db)),
        "all_projects": it.projects(st.db),
        "rate_limit": st.db.kv_get("rate_limit"),
        "running": st.runner.active(),
        "backoff": st.runner.backoff(),
        "window": windows.status(st.cfg.run_windows),
        "paused_all": paused_all(st.db),
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
    return inbox_page(request, project)


def inbox_page(request: Request, project: str | None, status: int = 200, error=None):
    db = db_(request)
    pid = get_project(request, project)["id"] if project else None
    rows = it.inbox(db, pid)
    groups = [(label, [r for r in rows if r["state"] == s]) for s, label in INBOX_GROUPS]
    busy = db.all("SELECT items.*, projects.slug AS project_slug FROM items JOIN projects ON "
                  "projects.id = project_id WHERE state IN ('planning', 'implementing', "
                  "'merging') ORDER BY state_since")
    proposals = it.open_proposals(db, pid)
    targets = {p: (it.groups(db, p), it.default_group(db, p))
               for p in {r["project_id"] for r in proposals}}
    return render(request, "inbox.html", status, groups=groups, busy=busy, proposals=proposals,
                  targets=targets, project_filter=project, error=error)


def get_proposal(request: Request, proposal_id: int):
    row = db_(request).one("SELECT * FROM proposals WHERE id = ?", proposal_id)
    if row is None:
        raise HTTPException(404, f"no proposal {proposal_id}")
    return row


@router.post("/proposals/{proposal_id}/{action}")
async def proposal_action(request: Request, proposal_id: int, action: str):
    """Accept (add to the roadmap, in the chosen group) or dismiss a proposed item (§6)."""
    get_proposal(request, proposal_id)
    form = await request.form()
    nxt = str(form.get("next") or "/")
    try:
        if action == "accept":
            gid = str(form.get("group_id", ""))
            it.accept_proposal(db_(request), proposal_id, int(gid) if gid else None)
        elif action == "dismiss":
            it.dismiss_proposal(db_(request), proposal_id)
        else:
            raise HTTPException(404, f"no action {action}")
    except ValueError as e:
        project = str(form.get("project") or "") or None
        return inbox_page(request, project, 409, error=str(e))
    request.app.state.runner.publish("-")
    return back(nxt)


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
                  groups=it.groups(db, p["id"]), default_group=it.default_group(db, p["id"]),
                  imports=it.imports(db, p["id"]), needs_key=bool(key and not (key["access"] or {}).get("ok")))


@router.post("/p/{slug}/import")
async def project_import(request: Request, slug: str):
    p = get_project(request, slug)
    form = await request.form()
    try:
        item_id = await asyncio.to_thread(act.start_import, request.app.state.runner, p["id"],
                                          str(form.get("instructions", "")))
    except act.ActionError as e:
        return render(request, "project.html", 409, project=p, show="active", unmet={},
                      rows=it.items(db_(request), p["id"], active_only=True),
                      groups=it.groups(db_(request), p["id"]),
                      default_group=it.default_group(db_(request), p["id"]),
                      imports=it.imports(db_(request), p["id"]), error=str(e))
    return back(f"/i/{item_id}")


@router.post("/p/{slug}/items")
async def quick_add(request: Request, slug: str):
    p = get_project(request, slug)
    form = await request.form()
    gid = str(form.get("group_id", ""))
    try:
        it.add_item(db_(request), p["id"], str(form.get("title", "")),
                    str(form.get("description", "")), top=bool(form.get("top")),
                    group_id=int(gid) if gid else None)
    except ValueError as e:
        return render(request, "project.html", 400, project=p, show="active", unmet={},
                      rows=it.items(db_(request), p["id"], active_only=True),
                      groups=it.groups(db_(request), p["id"]),
                      default_group=it.default_group(db_(request), p["id"]),
                      imports=it.imports(db_(request), p["id"]), error=str(e))
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
    request.app.state.runner.publish("-")  # e.g. unpaused: wake the scheduler
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
    preview = preview_errors = notes = None
    if item["type"] == "import" and plans:
        try:
            preview = roadmap.parse(plans[0]["markdown"], proj["slug"])
        except RoadmapError as e:
            preview_errors = e.errors
        last = db.one("SELECT output_json FROM runs WHERE item_id = ? AND status = 'succeeded' "
                      "ORDER BY id DESC LIMIT 1", item_id)
        notes = (json.loads(last["output_json"]) or {}).get("notes") if last else None
    return dict(preview=preview, preview_errors=preview_errors, notes=notes,
                groups=it.groups(db, proj["id"]),
                item=item, project=proj, plans=plans, plan_diff=plan_diff, rounds=rounds,
                open_round=open_round, impl=impl, diff=diff, diffstat=diffstat, log=log,
                runs=runs, current=current, deps=deps, events=events,
                live_lines=run_lines(current) if current else [],
                unmet=it.unmet_dependencies(db, item, need) if item["state"] in
                ("backlog", "approved") else [],
                busy=st.runner.busy(item_id), branch=g.branch_name(item_id),
                total_cost=sum(r["cost_usd"] or 0 for r in runs),
                proposals=db.all("SELECT * FROM proposals WHERE source_item_id = ? ORDER BY id",
                                 item_id))


@router.get("/i/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: str):
    return render(request, "item.html", **item_context(request, item_id))


@router.post("/i/{item_id}/edit")
async def item_edit(request: Request, item_id: str):
    get_item(request, item_id)
    form = await request.form()
    deps = [d.strip() for d in str(form.get("depends", "")).replace(",", " ").split() if d.strip()]
    gid = str(form.get("group_id", "keep"))
    try:
        it.edit_item(db_(request), item_id, str(form.get("title", "")),
                     str(form.get("description", "")), deps,
                     kind=str(form["kind"]) if "kind" in form else None,
                     group_id="keep" if gid == "keep" else int(gid) if gid else None)
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


# --- system --------------------------------------------------------------------------------

RECENT = 50
TOKENS = (("in", "input_tokens"), ("out", "output_tokens"),
          ("cache read", "cache_read_input_tokens"), ("cache write", "cache_creation_input_tokens"))


def tokens(usage_json: str | None) -> dict[str, int]:
    """The token counts in a run's `usage_json`; anything missing or odd is left out."""
    try:
        usage = json.loads(usage_json) if usage_json else None
    except json.JSONDecodeError:
        return {}
    if not isinstance(usage, dict):
        return {}
    return {label: usage[k] for label, k in TOKENS if isinstance(usage.get(k), int)}


def system_context(request: Request) -> dict[str, Any]:
    st = request.app.state
    db = st.db
    run_cols = ("SELECT runs.*, items.title, projects.slug AS project_slug FROM runs "
                "LEFT JOIN items ON items.id = runs.item_id "
                "LEFT JOIN projects ON projects.id = items.project_id ")
    current = db.all(run_cols + "WHERE runs.status IN ('queued', 'running') ORDER BY runs.id")
    errors = [dict(when=r["ended_at"] or r["started_at"], item_id=r["item_id"], title=r["title"],
                   what=f"{r['kind']} {r['status']}", status=r["status"], message=r["error"],
                   href=f"/runs/{r['id']}")
              for r in db.all(run_cols + "WHERE runs.status IN ('failed', 'interrupted', "
                              "'rate_limited') ORDER BY runs.id DESC LIMIT ?", RECENT)]
    for e in db.all("SELECT events.*, items.title FROM events LEFT JOIN items ON "
                    "items.id = events.item_id WHERE events.kind IN ('readonly_violation', "
                    "'dirty_worktree') ORDER BY events.id DESC LIMIT ?", RECENT):
        errors.append(dict(when=e["ts"], item_id=e["item_id"], title=e["title"], what=e["kind"],
                           status=None, message=(fromjson(e["data_json"]) or {}).get("message"),
                           href=f"/i/{e['item_id']}"))
    errors.sort(key=lambda x: x["when"] or "", reverse=True)
    usage = [(r, tokens(r["usage_json"])) for r in
             db.all(run_cols + "ORDER BY runs.id DESC LIMIT ?", RECENT)]
    totals = []
    for label, since in (("24 h", timedelta(days=1)), ("7 days", timedelta(days=7)),
                         ("all time", None)):
        cutoff = (datetime.now(UTC) - since).isoformat(timespec="seconds") if since else ""
        row = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost FROM runs "
                     "WHERE COALESCE(started_at, '') >= ?", cutoff)
        totals.append((label, row["n"], row["cost"]))
    return dict(current=current, busy={r["item_id"] for r in current
                                       if r["item_id"] and st.runner.busy(r["item_id"])},
                errors=errors[:RECENT], usage=usage, totals=totals,
                paused_projects=[p for p in it.projects(db) if p["paused"]])


@router.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    return render(request, "system.html", **system_context(request))


@router.post("/system/pause")
async def system_pause(request: Request):
    form = await request.form()
    set_paused_all(db_(request), form.get("on") == "1")
    request.app.state.runner.publish("-")  # e.g. resumed: wake the scheduler
    return back("/system")


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
