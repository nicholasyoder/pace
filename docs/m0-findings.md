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
- **Plan → skip-permissions resume:** NOT YET TESTED. Running `--dangerously-skip-permissions` as
  `dev` is (rightly) blocked; this has to happen as `pace-agent` (Step 2).

Fixtures: `tests/fixtures/stream/plan_needs_input.jsonl`, `plan_ready_resumed.jsonl`.
Schemas: `pace/schemas/{plan,implement}.json`.

## Open
- Plan → implement resume across permission modes (as pace-agent).
- §15.2 headless auth for pace-agent.
- §15.3 git isolation (upload-pack reads the agent repo's config; consider bundles).
- Agent `~/.claude` contents; per-project auto-memory dir is created under `~/.claude/projects/<slug>/memory`.
