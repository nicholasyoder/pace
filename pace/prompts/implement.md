Your plan for {{item_id}} is approved. Implement it now. You have full permissions in this worktree; there is no human in this conversation.

- The plan was written against commit {{plan_base}}; the branch `{{branch}}` now starts at {{impl_base}}. If the code has moved on, adapt.
- Commit your work in logical commits on `{{branch}}`. Leave the worktree clean (no untracked build or cache files).
- Test as far as you can: build, run the test suite, exercise the program headlessly if feasible.
- `verified`: exactly what you ran or checked, and the result. Don't claim anything you didn't do.
- `manual_checks`: steps only a human can do (launch X, look at Y, expect Z), concrete enough to follow without context.
- `commit_message`: used for the squash merge; a subject line, a blank line, then the body.
- If you can't complete the work, set `status` to `blocked` and explain in `blocked_reason`.

{{extra_instructions}}
