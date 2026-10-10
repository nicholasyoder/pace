# PACE — Spec (v0.5)

**PACE** (Plan · Approve · Code · Evaluate). A personal, self-hosted orchestrator that turns per-project roadmaps into a steady pipeline of plans and implementations driven by Claude Code, and only interrupts me when a decision or a manual check is needed.

Built for one user and one setup (a Linux VM reached over Tailscale). It is not a general-purpose tool. When in doubt, pick the simpler option.

---

## 1. Goals and principles

**The workflow PACE replaces.** Open Claude Code → "plan how to proceed with item X" → answer Claude's questions → approve the plan → Claude implements → get test steps → test → merge. PACE automates every step that isn't a human decision or a human check.

**Principles**

1. **Deterministic core.** State transitions, scheduling, git and process management are plain code. The only LLM is Claude Code. It reports through structured output and code decides what happens next.
2. **Claude Code does all the thinking.** Planning, implementing, self-testing, summaries, commit messages and manual test steps. PACE spawns the real `claude` binary headlessly and never touches its auth or calls Anthropic APIs.
3. **Verification belongs to Claude, then to me.** PACE runs no test commands. Claude tests as far as it can while implementing and reports what it verified. Anything that needs a human (launch the app, look at the UI) comes back as manual check steps.
4. **Inbox, not firehose.** The default view is "things that need me", oldest first.
5. **Structural isolation** (§11). Claude runs with permission prompts off, so it can't be trusted with anything PACE holds.
6. **Minimal.** No feature goes in unless I need it.

**Non-goals:** multi-user, other coding agents, configurability for other setups, CI-style automated checks, a mobile app (the web UI must still work well on a phone).

---

## 2. Architecture

```
 Browser ──HTTP/SSE──► PACE server (one Python process, runs as user `pace`)
                         ├─ Web UI (server-rendered + HTMX)
                         ├─ Scheduler loop (asyncio)
                         ├─ Run manager ── sudo -u pace-agent -D <wt> ──► claude -p (per item, in an agent-owned worktree)
                         ├─ Git (bare canonical clone per project; bundle-in / merge / push / gh)
                         └─ SQLite
```

One process, one SQLite DB, background work as asyncio tasks.

---

## 3. Stack

- **Python ≥ 3.12 (the VM has 3.13), `uv`, FastAPI + Uvicorn.**
- **Jinja2 + HTMX** (vendored) and one hand-written CSS file (light/dark from the system setting, responsive). No JS build step. Live updates over **SSE**.
- **SQLite (WAL)** through stdlib `sqlite3` with plain SQL. Migrations are numbered `.sql` files applied at startup.
- `markdown-it-py` renders markdown. Diffs come from `git diff` with simple +/- colouring.
- The `gh` CLI handles PRs (authenticated as `pace`). Requires git ≥ 2.38 for `merge-tree --write-tree`.
- `pytest`, plus a `fake_claude` script that emits canned stream-json, so tests spend no quota.

```
pace/
  main.py config.py db.py web.py bus.py items.py actions.py
  scheduler.py runs.py agent.py git.py roadmap.py notify.py
  prompts/ schemas/ templates/ static/ migrations/
tests/ (fake_claude, fixtures/stream/)
scripts/setup-vm.sh scripts/agent-CLAUDE.md
/var/lib/pace/                 # data dir (config), owned by pace, 0711
  pace.db
  logs/<run-id>.jsonl.gz       # 0700
  bundles/                     # branch transfer files
  keys/<slug>_ed25519          # per-project GitHub deploy keys, 0700
  repos/<slug>.git             # bare canonical clone, 0755: agent-readable, pace-writable
/home/pace-agent/work/<slug>/  # agent-owned, private (0700)
  repo.git                     # bare clone of the canonical repo, remote `canon`
  wt/<ID>                      # worktree on branch pace/<ID>
```

---

## 4. Concepts

- **Project**: a codebase, its roadmap and its settings.
- **Item**: one roadmap entry, with a stable ID like `WEB-014` that is never reused.
- **Run**: one `claude` invocation for an item. Kinds: `plan`, `import` (§6), `answers` (the follow-up after I answer questions), `revise_plan`, `implement`, `address_feedback`, `resolve_conflicts`, and `import` (not tied to an item).
- **Inbox entry**: derived from item state when I need to act (§7).

---

## 5. Configuration

**Global `config.toml`:** data dir, bind address, agent user name, ntfy URL, global concurrency cap, run windows (§9), per-run timeout. It holds no secrets: git push uses per-project deploy keys (or `pace`'s own SSH setup) and PRs use `pace`'s `gh auth`.

**Project settings** (stored in the DB, edited with a UI form):

| Field | Default | |
|---|---|---|
| `slug` | | Item ID prefix: `WEB-001` |
| `name`, `origin`, `base_branch` | | `origin` must be pushable by `pace`. A github.com origin (`owner/repo`, or its https/SSH URL, normalized to `git@github.com:owner/repo.git`) gets a PACE-generated deploy key (§11). Any other origin (an SSH alias, a local bare repo) uses `pace`'s own SSH setup |
| `merge_mode` | `local` | `local` (PACE merges and pushes the base branch) or `pr` |
| `merge_strategy` | `squash` | `squash` or `merge` |
| `plan_ahead` | 3 | Max items in `planning` + `needs_input` + `plan_ready` |
| `test_ahead` | 2 | Max items in `ready_to_test` before more implementations start |
| `max_concurrent_implementations` | 1 | |
| `model` | "" | Empty means the Claude Code default |
| `extra_instructions` | "" | Appended to every prompt for this project |

---

## 6. Roadmap

**The DB is canonical.** The markdown format below is used for import, the "edit as markdown" view and export. The repo's own roadmap docs aren't touched; they stay as narrative and history.

```markdown
Optional preamble: roadmap-wide context, sent with every planning run.

## WEB-002: An ungrouped item (ungrouped items come first)

# v0.4 — Reporting
Optional group description: context for every item in the group.

## WEB-014: Add CSV export for reports
- depends: WEB-003, WEB-009
- kind: debt
- parked

Free-form markdown description: context, constraints, acceptance criteria.
Everything until the next `## ` or `# ` heading belongs to the item;
headings inside it must be `###` or deeper.
```

- **Order is priority:** groups in order, then items within each group. Reorder to reprioritise; "move to top" bumps an item to the top of its group.
- **Groups** are tiers, releases or milestones; PACE treats them all the same. A group's description and the preamble go into every plan prompt for its items. Groups with no active items are hidden from the active view, and a group nothing refers to is deleted on save.
- **Metadata lines** come straight after the item heading, each at most once:
  - `- depends:`
  - `- kind:`, one lowercase word such as `debt`, shown as a badge
  - `- parked`, which parks the item when the markdown is saved or imported
- **Strict parser, no LLM.** A heading without an ID becomes a new item and gets the next ID (each project keeps a `next_id` counter). If a save leaves out an existing ID, the UI asks whether to park that item. Parse errors are shown with line numbers and nothing is saved until the markdown is valid.
- **Editing:** a structured list (add, edit, reorder within a group, park; the edit form sets group and kind) plus a raw markdown view of the active items.
- **Quick add:** one line becomes an item with that title, added to a chosen group (by default the last visible one). If it's underspecified, the planning run will ask.
- **Import** is an *import item* (`type = 'import'`, ID `<SLUG>-IMPORT-<n>`) that goes through the normal plan lifecycle:
  - **Starting it:** "Import roadmap" on the project page, with optional notes (where to look, what to leave out, or a pasted roadmap). This starts an `import` run: read-only Claude in a worktree, with schema `import.json` (`roadmap_markdown`, `notes`, `questions`).
  - **What Claude produces:** it reads the repo's roadmap material and skips done work. It splits umbrella entries into item-sized ones, maps tiers and releases to groups, and asks questions where the conversion needs a decision.
  - **Validation:** the proposal must pass the strict parser. IDs are provisional (`next_id` onwards), and dependencies may also point at existing items. A proposal that fails gets one retry, with the parser errors sent back to Claude.
  - **Review:** the item page shows Claude's notes and a structured preview, grouped, with kinds, dependencies and parked items. Feedback and answers resume the session.
  - **Applying:** "Apply to roadmap" adds the items after the existing roadmap with fresh IDs and dependencies remapped. A non-empty preamble replaces the project's. The import then moves straight from `plan_ready` to `merged`; nothing is committed.
  - **Visibility:** imports never appear in the roadmap list or the markdown view.
- **Proposed items:** any run can propose new items or splits (§8.2). They appear in the inbox, where I accept them (they're added to the roadmap) or dismiss them.

---

## 7. Item lifecycle

| State | Meaning | What moves it on |
|---|---|---|
| `backlog` | Not started | Scheduler |
| `planning` | A `plan` / `revise_plan` run is in progress | Run completion |
| `needs_input` | Claude has questions | **Me**: answer → `planning` |
| `plan_ready` | Plan awaiting approval | **Me**: approve → `approved`; feedback → `planning`; park → `parked` |
| `approved` | Waiting for an implementation slot | Scheduler |
| `implementing` | An `implement` / `address_feedback` / `resolve_conflicts` run is in progress | Run completion |
| `ready_to_test` | Branch pushed (and PR open in PR mode) | **Me**: "Works → merge" → `merging`; "Problem…" + note → `changes_requested` |
| `changes_requested` | I reported a problem | Scheduler (goes ahead of other work) |
| `merging` | Merge in progress | Deterministic; conflicts → `resolve_conflicts` run |
| `merged` | Done; hidden from the active roadmap | — |
| `failed` | Retries exhausted, contract violated, or Claude reported itself blocked | **Me**: retry, edit, or park |
| `parked` | Set aside | **Me**: unpark → `backlog` |

**Dependencies:** an item can be planned once all its dependencies are at least `approved`; their plans go to Claude as context. It can be implemented only once they are all `merged`. Unmet dependencies are not a state: the scheduler skips the item and its page shows "waiting on WEB-003".

**Inbox** = `needs_input`, `plan_ready`, `ready_to_test`, `failed`, plus proposed items. Each card shows age and Claude's summary.

**Stale plans:** when an item merges, any `plan_ready` or `approved` item whose plan lists `affected_files` that the merge changed is flagged "plan may be stale", with the overlapping files listed. I can trigger a `revise_plan` run from there. Nothing is discarded automatically.

---

## 8. Claude runs

### 8.1 Invocation

The run is spawned as the agent user (flags verified in M0 with `claude` 2.1.296). sudo's default `env_reset` and `secure_path` scrub the environment. PACE spawns sudo with cwd `/`, because the agent can't stat PACE's directories, and sets Claude's cwd with `-D`. PACE itself can't enter the agent's private worktree, and `claude` has no `--cwd` flag.

```
sudo -n -u pace-agent -H -D <worktree> ~pace-agent/.local/bin/claude \
    -p "<prompt>" --output-format stream-json --verbose \
    --json-schema '<schema JSON for this run kind>' \
    --disallowedTools AskUserQuestion \
    [--resume <session_id>] [--model …] \
    [--permission-mode plan]          # plan / revise_plan / import: read-only
    [--dangerously-skip-permissions]  # implement / address_feedback / resolve_conflicts
```

- **Results come from structured output only.** Claude reports by calling a `StructuredOutput` tool. PACE reads `structured_output` (and `session_id`, `usage`, `subtype`, `is_error`) from the final `result` event. `total_cost_usd` is cumulative per session across resumes, so store per-run deltas.
- In plan mode Claude doesn't call `ExitPlanMode`, but it may write a plan file to `~/.claude/plans/` in the agent's home. That's harmless.
- **Resume** works across permission modes (plan → bypass) and even from a different cwd. PACE still keeps the worktree path stable.
- **Read-only check:** after every read-only run, the worktree must be clean. If it isn't, reset it, log an event and flag it in the UI.
- **Stream events** are written to a gzipped log and sent over SSE to the item's live activity pane.
- **Sessions:** the first run stores its `session_id`, and every later run on the item resumes that session in the same worktree path. If resume fails, start a fresh session and put the plan, the Q&A history and the feedback into the prompt.
- **Invalid or missing structured output** gets one automatic retry that says what was wrong. After that the item goes to `failed`.
- **Rate limits / quota:** every run emits `rate_limit_event`s whose `rate_limit_info` has `status` (`allowed` normally), `resetsAt`, and `unifiedWindows.{five_hour,seven_day}.{utilization,resetsAt}`. PACE stores the latest one. The scheduler stops starting runs while utilization is above a configured threshold, or while `status` isn't `allowed`, until `resetsAt`, and the UI shows a banner. A run that fails on a limit anyway is marked `rate_limited` and the item returns to its prior state. The exact failure shape is still unseen, so detect it loosely.
- Every run has a hard timeout. Timeouts and **Stop run** send SIGTERM to sudo, which relays it to `claude`, which kills its tool processes and exits (verified in M1). SIGKILL comes only after a 15 s grace period, because it would kill just sudo and orphan `claude` under the agent user, where PACE can't signal it.
- `--model` doesn't apply to plan mode: with `--model haiku`, plan runs still reported Sonnet usage.
- **Retry** re-runs the failed run's kind with the same inputs (stored in `runs.input_json`). A failed merge retries the merge.

### 8.2 Output contracts (JSON schemas)

**Plan runs** (`plan`, `revise_plan`):
```json
{ "status": "needs_input | plan_ready | blocked",
  "summary": "1–2 sentences for the inbox card",
  "plan_markdown": "Sections: Summary, Approach, Affected areas, Steps, Risks, Verification plan, Decisions",
  "affected_files": ["src/…"],
  "questions": [{ "id": "q1", "text": "…", "why_it_matters": "…",
                  "options": [{"id": "a", "label": "…", "description": "…"}],
                  "recommended": "a", "allow_free_text": true }],
  "blocked_reason": null,
  "proposed_items": [{ "title": "…", "description": "…", "reason": "…" }] }
```

**Implementation runs** (`implement`, `address_feedback`, `resolve_conflicts`):
```json
{ "status": "done | blocked",
  "summary": "…",
  "commit_message": "used for the squash merge and the PR title/body",
  "verified": ["what Claude ran or checked, and the result"],
  "manual_checks": ["steps only a human can do: launch X, look at Y, expect Z"],
  "notes": "…", "blocked_reason": null,
  "proposed_items": [] }
```

**Import run:** `{ "roadmap_markdown": "…" }`, which is then validated by the strict parser.

Each plan version is stored, so plan review can show the changes since the previous version.

### 8.3 Prompts

Templates live in `prompts/*.md` with `{{variables}}`: `plan`, `answers`, `revise_plan`, `implement`, `address_feedback`, `resolve_conflicts`, `import`. `answers` and `revise_plan` are short follow-ups sent into the resumed session. Every prompt includes the item, the plans of its dependencies, a reminder of the contract, and the project's `extra_instructions`. Key instructions:

- **plan:** only ask questions that would change the implementation and can't be answered from the code; give a recommended option for each. Propose items when something should be split off or is out of scope.
- **implement:** follow the approved plan (written against commit X; adapt if the code has moved on). Commit in logical commits on the item branch. Test as far as you can: build, run the tests, exercise the app headlessly if feasible. Report exactly what you verified, and list everything that still needs human eyes as `manual_checks`.

---

## 9. Scheduler

A loop runs every ~10 s and on every state change. It only starts runs inside a **run window** (e.g. `run_windows = ["22:00-08:00"]`; empty means always) and outside rate-limit backoff (§8.1). Runs already in progress always finish.

1. `changes_requested` → start `address_feedback`.
2. `approved` → start `implement`, in roadmap order, if dependencies are merged, an implementation slot is free and `ready_to_test` < `test_ahead`.
3. While `planning` + `needs_input` + `plan_ready` < `plan_ahead`, plan the next `backlog` item in roadmap order whose dependencies are at least `approved`.
4. Respect the global concurrency cap.
5. More than 3 attempts in one state → `failed`, with the reason.

**On startup:** any run still marked `running` becomes `interrupted`, its sudo PID gets SIGTERM if still alive, and the item is re-queued to resume its session. (M1 sends the item to `failed` and leaves the rest to Retry; M2 re-queues automatically.)

**Before M2:** human actions start the follow-up run directly when a slot is free (Plan now, answers, feedback, Approve → implement, Problem → `address_feedback`). If no slot is free, the item waits in `approved` / `changes_requested` with a button to start it.

**Controls:** pause project, pause all, plan now, move to top, park, retry.

---

## 10. Git

**Repos**
- **Canonical:** `repos/<slug>.git`, a bare clone owned by `pace` whose `refs/heads/*` mirror origin (fetch refspec `+refs/heads/*:refs/heads/*`, `--prune`). **PACE never runs git in a repo the agent can write to**, and it can't read the agent's repos at all.
- **Agent:** `~pace-agent/work/<slug>/repo.git`, a bare clone of the canonical repo (remote `canon`, refspec `+refs/heads/*:refs/remotes/canon/*`), with worktrees at `wt/<ID>` on branch `pace/<ID>`. The agent trusts the canonical repos through `safe.directory=/var/lib/pace/repos/*` in its global git config.
- **Agent → PACE transfer is a bundle**, never a fetch. A fetch would run `upload-pack` against agent-written config. Instead, as the agent: `git bundle create - pace/<ID> ^canon/<base>` to stdout. PACE writes it to `bundles/`, runs `bundle verify`, then `fetch <file> +refs/heads/pace/<ID>:refs/heads/pace/<ID>`.

**Flow**
1. **Plan start:** `pace` fetches origin into the canonical repo. Then, as the agent: fetch `canon` and `worktree add -B pace/<ID> wt/<ID> canon/<base>`. Record `plan_base_commit`.
2. **Implement start:** fetch again; as the agent, run `git checkout -B pace/<ID> canon/<base>` in the worktree (it's clean after planning, and the path stays the same). Record `impl_base_commit`. A `done` result with no new commits is a contract violation.
3. **After implementation:** bundle `pace/<ID>` into the canonical repo (above) and push it to origin in both modes, so I can test from any machine. In PR mode, it also runs `gh pr create --draft` with a body built from the summary and manual checks. → `ready_to_test`.
4. **Works → merge:** fetch origin, then `git merge-tree --write-tree origin/<base> pace/<ID>`:
   - **Conflicts:** start a `resolve_conflicts` run (the agent merges the new base into its branch), then return to `ready_to_test` flagged "re-verify".
   - **Clean, local mode:** `commit-tree` (squash: parent = base, message = `commit_message`; merge: two parents), then push to `origin/<base>`. If the push is rejected as non-fast-forward, start again from the fetch.
   - **Clean, PR mode:** `gh pr ready`, then `gh pr merge --squash|--merge`.
5. **After merge:** delete the remote branch, remove the worktree (as the agent), mark the item `merged`, and run the stale-plan check.
6. **PR mode:** poll open PRs every few minutes. If a PR is merged or closed outside PACE, the item moves to `merged` or `parked`.

---

## 11. Security

- The UI binds to the configured address (default `127.0.0.1`; in practice the Tailscale IP). There is no app auth.
- Claude runs as **`pace-agent`** (password locked, home 0700):
  - It has no SSH keys, no `gh` auth, and no read access to the data dir except `repos/` (read-only).
  - Its only git remote is the canonical bare repo.
  - Sudoers: `pace ALL=(pace-agent) CWD=* NOPASSWD: /home/pace-agent/.local/bin/claude, /usr/bin/git`. There's no `env` wrapper, which would allow anything. `CWD=*` permits `-D`.
  - It has its own native `claude` install (auto-updates) and is logged in once with an interactive `/login` as `pace-agent`. Credentials refresh themselves, and no token passes through PACE.
  - Its global `~/.claude/CLAUDE.md` comes from `scripts/agent-CLAUDE.md`: headless, no human reading; install tools into its home (uv, `pip --user`), never sudo; don't switch branches or push; leave the worktree clean; verify precisely. Auto-memory stays on.
- PACE never executes code from agent directories. On the agent side it only runs `git` and `claude` through sudo; on its side it works only in its own bare repos and bundle files.
- All Claude-produced text is rendered escaped.
- `scripts/setup-vm.sh` (idempotent, run as root) sets up a VM:
  - the system packages (including project build dependencies the agent can't install itself);
  - the `pace` user with `uv`;
  - the agent user with its own `claude`, Rust and `uv`;
  - the sudoers rule, the agent's CLAUDE.md, the data dir and `/etc/pace/config.toml`.
- `scripts/deploy.sh` resets `/opt/pace` to `origin/main` with the PACE project's own deploy key, runs `uv sync`, installs `pace.service`, and restarts once no runs are in progress.
- `scripts/export-data.sh` and `import-data.sh` move the DB, keys and logs between VMs. See DEPLOY.md. Push credentials are per-repo deploy keys. For a GitHub project, PACE generates an ed25519 key in `keys/` when the project is saved. Every origin operation uses it through `core.sshCommand` in the canonical repo (`-c` for the first clone), so `~/.ssh/config` is never edited. The settings page shows the public key with a link to the repo's "add deploy key" page. **Check access** runs `ls-remote` and then a dry-run push, so it catches a key added without write access, and nothing is written. Until the check passes, the project page shows a setup banner.

---

## 12. Data model (SQLite)

- `projects(id, slug, name, origin, base_branch, merge_mode, merge_strategy, plan_ahead, test_ahead, max_concurrent_implementations, model, extra_instructions, preamble, next_id, paused)`
- `groups(id, project_id, position, title, description)`
- `items(id TEXT PK, project_id, type[item|import], group_id, kind, position, title, description, depends_json, state, state_since, attempts, session_id, branch, plan_base_commit, impl_base_commit, pr_url, stale_json, reverify, summary, last_error, created_at, updated_at)`
- `plans(item_id, version, markdown, affected_files_json, created_at)`
- `qa_rounds(item_id, round, questions_json, answers_json, answered_at)`
- `runs(id, item_id, kind, input_json, status[queued|running|succeeded|failed|interrupted|rate_limited], pid, session_id, started_at, ended_at, exit_code, cost_usd, usage_json, output_json, log_path, error)`
- `proposals(id, project_id, source_item_id, title, description, reason, status)`
- `events(id, item_id, ts, kind, actor[system|human|claude], data_json)`: an append-only audit log that includes feedback text and approvals.

Every state transition is a single transaction that also writes an `events` row.

---

## 13. UI

1. **Inbox** (default; all projects, with a project filter). Groups: *Questions*, *Plans to approve*, *Ready to test*, *Problems*, *Proposed items*. The count shows in the nav and the page title.
2. **Item page**:
   - Header with state and the primary action.
   - **Questions:** radio options with the recommended one highlighted (not pre-selected), "why it matters", a free-text field, and "let Claude decide the rest".
   - **Plan:** rendered plan, diff against the previous version, and Approve / Feedback / Park.
   - **Test & merge:** summary, what Claude verified, the manual checks as a checklist, the diff, the branch/PR with a checkout command, and Works → merge / Problem….
   - **Activity:** events and runs, with the live stream for the current run.
3. **Project page:** the roadmap as an ordered list with state badges, plus reorder, quick add, edit, park, edit as markdown, import, the settings form and pause.
4. **System:** current runs, backoff and run-window status, recent errors, usage per run.

**Notifications:** an ntfy push when the inbox gains entries, at most one every N minutes, with a deep link.

---

## 14. Milestones (each must be usable)

- **M0 — Spike (CLI, no UI). ✓ Done 2026-10-10.** `python -m pace.spike` took a toy item and nkms #24 through plan → questions → approve → implement as `pace-agent` → push → `merge-tree` squash merge. Findings are folded into this version. `fake_claude` replays recorded runs. `pace/agent.py`, `pace/git.py`, `pace/prompts/` and `pace/schemas/` carry into M1. `pace/spike.py` gets deleted once M1 replaces it.
- **M1 — Core, end to end in local mode. ✓ Built 2026-10-10.** DB, roadmap parser and markdown editing, project add, state machine, run manager, inbox and item page, SSE, local merge, `address_feedback` and `resolve_conflicts`. It was proven with real `claude` as `pace-agent` on a toy repo through plan → implement → Problem → fix → squash merge. Not in M1: the System page (moves to M2) and the proposal inbox actions (M3; proposals are stored and shown on the item page, deduped by title). Runs as `dev` via `uv run python -m pace` until the `pace` user and a systemd unit arrive at the end of M2.
- **M2 — Scheduler.** Pipeline, dependencies, `test_ahead`, run windows, backoff, restart recovery, attempt caps, the System page, and the `pace` user plus a systemd unit.
- **M3 — The rest.** PR mode (first check `gh pr ready` / `gh pr merge` behaviour on draft PRs), ntfy, proposed items, stale-plan flag. (Import, groups, kinds and the preamble came forward and were built 2026-10-10.) Runs often re-propose items an earlier run already proposed, so dedupe by title against open and dismissed proposals.
- **M4 — Phone pass.** Layout polish for phone use.

**Acceptance:** with two projects (one `local`, one `pr`), I import a roadmap and leave PACE running. Later I find plans and questions in the inbox, answer and approve them, and later still find branches ready to test with clear manual checks. I can merge both ways from the UI and never need a terminal.
