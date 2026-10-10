The reviewer tested branch `{{branch}}` for {{item_id}} and reported a problem:

{{feedback}}

Fix it on `{{branch}}`. You have full permissions in this worktree; there is no human in this conversation.

- Add commits on top of the branch; don't rewrite its history. Leave the worktree clean.
- Test the fix as far as you can, and re-check what the earlier work verified if the fix could affect it.
- `verified` and `manual_checks` replace the previous ones, so cover the whole change, not just the fix.
- `commit_message` describes the whole change on the branch (it's used for the squash merge).
- If you can't fix it, set `status` to `blocked` and explain in `blocked_reason`.

{{extra_instructions}}
