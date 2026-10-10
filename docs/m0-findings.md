# M0 findings (working notes — fold into spec v0.3, then delete)

Probed with `claude` 2.1.296 on 2026-10-10, as `dev`, on a toy repo.

## §15.1 claude CLI

- **`-p --output-format stream-json --verbose`** works. Event types seen: `system/init`
  (has `cwd`, `model`, `permissionMode`, `apiKeySource`), `assistant` (thinking / text /
  tool_use blocks), `user` (tool_result), `system/thinking_tokens`, `rate_limit_event`, `result`.
- **`--json-schema`** works. Claude reports by calling a `StructuredOutput` tool. The parsed object is
  in `result.structured_output`; `result.result` holds the same JSON as a string.
  `result.subtype == "success"`, `is_error`, `terminal_reason`, `num_turns`, `permission_denials`.
- **Cost:** `total_cost_usd` is **cumulative per session** across resumes (0.23 → 0.26 → 0.29),
  so store per-run deltas or just `usage`. `usage` looks per-invocation.
- **Headless `--permission-mode plan`** works. Claude did not call `ExitPlanMode`, it went straight
  to `StructuredOutput`. It **did write a plan file to `~/.claude/plans/*.md`** (outside the
  worktree, which stayed clean). That's harmless under pace-agent's home, but it's a write.
- **`--disallowedTools AskUserQuestion`**: Claude asked through the schema's `questions` instead.
  Good-quality question with options + recommended.
- **`--resume <id>`** in plan mode with answers → `plan_ready`, same `session_id`.
- **Resume from a different cwd works** and keeps the context (sessions are found by ID even though the
  transcript lives under `~/.claude/projects/<cwd-slug>/`). Keeping the worktree path stable is
  still sensible, but it's not required.
- **Rate limits:** every run emits `rate_limit_event` with
  `rate_limit_info = {status: "allowed", rateLimitType, resetsAt, overageStatus, isUsingOverage,
  unifiedWindows: {five_hour: {utilization, resetsAt}, seven_day: {utilization, resetsAt}}}`.
  → **Proactive backoff is possible:** the scheduler can stop starting runs above a utilization
  threshold and wait until `resetsAt`, instead of only reacting to failures. The rejected shape is still unseen
  (presumably `status != "allowed"`).
- **Plan → skip-permissions resume works** (as pace-agent, `sudo -D <wt>`): same session_id,
  init shows `permissionMode: bypassPermissions`, Claude committed in logical commits, cleaned up
  `__pycache__`, and reported honest `verified` / `manual_checks` / `proposed_items`.
- Claude's `commit_message` already carries a `Co-Authored-By` trailer (agent's default attribution).
- The agent's toolchain matters: pytest wasn't installed for pace-agent, so Claude fell back to
  calling the test functions directly and flagged it. Per-project toolchains (uv, cargo, …) need
  installing for pace-agent.

Fixtures (real pace-agent runs): `tests/fixtures/stream/{plan_needs_input,plan_ready,implement_done}.jsonl`.
Schemas: `pace/schemas/{plan,implement}.json`.

## Users, sudo, cwd (setup-vm.sh)
- pace-agent has its own native `claude` install (`~pace-agent/.local/bin/claude`, auto-updates);
  auth via interactive `/login` as pace-agent, so the credentials refresh themselves and no token goes through the env.
- sudo's default `env_reset` + `secure_path` already scrub the env → no `env -i` wrapper (which
  would have made `env` an allowed command, i.e. anything).
- sudo keeps the caller's cwd; the agent can't stat PACE's dirs → always spawn sudo with cwd=`/`.
- PACE can't chdir into the agent's 0700 worktree and claude has no `--cwd` → `sudo -D <worktree>`,
  which needs `CWD=*` on the sudoers rule.
- During M0 `dev` stands in for `pace` and has full sudo anyway; the scoped rule only bites once
  a real `pace` user exists.

## §15.3 git isolation → bundles, not fetch
- Agent home is 0700, so PACE cannot read the agent clone at all — good: PACE's git never touches
  a repo the agent controls (a local fetch would run upload-pack against agent-written config).
- **Agent → PACE:** `sudo -u pace-agent git -C <wt> bundle create - pace/<ID> ^canon/<base>` to
  stdout; PACE writes it to a file it owns, `git bundle verify`, then
  `git fetch <file> +refs/heads/pace/<ID>:refs/heads/pace/<ID>` into the canonical repo. Tested ✓.
- **PACE → agent:** agent trusts canonical via `safe.directory=/var/lib/pace/repos/*` in its global
  git config (fine: canonical is PACE-owned and trusted).
- Agent layout: bare clone `~/work/<slug>/repo.git` (remote `canon`, refspec
  `+refs/heads/*:refs/remotes/canon/*`) with worktrees at `~/work/<slug>/wt/<ID>`.
- Local-mode merge: `merge-tree --write-tree origin/<base> pace/<ID>` → `commit-tree -p <base>`
  → push to origin. Tested ✓ against a local bare origin. (Commit identity needs setting for PACE.)

**Full loop proven on the toy repo:** plan → question → answer → plan_ready → implement →
bundle → canonical → push branch → merge-tree/commit-tree squash → push base → delete branch.

## Open
- Agent `~/.claude` contents; per-project auto-memory dir is created under `~/.claude/projects/<slug>/memory`.
