`{{branch}}` for {{item_id}} no longer merges cleanly into `{{base}}`, which has moved on. These files conflict:

{{conflicts}}

Merge `canon/{{base}}` into `{{branch}}` (it has just been fetched) and resolve the conflicts, keeping the intent of both sides. You have full permissions in this worktree; there is no human in this conversation.

- Use `git merge canon/{{base}}`; don't rebase. Commit the merge. Leave the worktree clean.
- Re-run the tests and re-check the behaviour the conflicting code affects.
- `verified` and `manual_checks` replace the previous ones and must cover the whole change; call out anything the resolution may have affected.
- `commit_message` describes the whole change on the branch (it's used for the squash merge).
- If you can't resolve it sensibly, set `status` to `blocked` and explain in `blocked_reason`.

{{extra_instructions}}
