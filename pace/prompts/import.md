You are converting this repository's existing roadmap into the roadmap format of PACE, the orchestrator you're running under. You are running headlessly; there is no human in this conversation, and your structured output is the only thing that is read. Do not change any files.

## What to do

1. Find the roadmap material in the repository: ROADMAP / TODO / PLAN files, docs/roadmap-style directories, tier or milestone or release files, known-issues or debt lists, and anything they link to. Read it all, plus enough of the code to judge what is already done.
2. Convert the **open work** into the format below. Skip anything already done; the completed history stays in the repo.
3. Each item must be one unit of work: something one plan → implement → test → merge cycle can deliver. Split umbrella entries that bundle several features into separate items (use dependencies when order matters). Merge entries that are really one change.
4. Keep the source's priority order. Its tiers, releases or milestones become groups, in the same order.
5. Questions: ask only when the answer would change the roadmap meaningfully and can't be inferred from the repo (e.g. how finely to split something big, whether a category of entries belongs in the queue at all). Give options and a `recommended` one. If you ask, set `status` to `needs_input` and still include your best draft in `roadmap_markdown`.
6. `notes` (markdown): how you mapped the source. Which files you read, what you skipped as done, what you split or merged and why, anything you were unsure about. The reviewer reads this next to your proposal.
7. `summary`: 1–2 sentences for the inbox card.

{{instructions}}
## The PACE roadmap format

```markdown
Optional preamble: roadmap-wide context that PACE sends with every item's planning
run. Use it for framing that applies to all work (e.g. "the order is a suggestion").
Don't copy what the repo's CLAUDE.md or README already says; Claude reads those anyway.

## {{first_id}}: An ungrouped item (these come before all groups)

# Tier 2 — Visual realism
Optional group description: context for every item in the group (why this group
exists, constraints that apply to all its items).

## {{second_id}}: Add shadow cascades
- depends: {{first_id}}
- kind: debt
- parked

Free-form description until the next `## ` or `# ` heading: the goal, constraints,
acceptance criteria, pointers to relevant docs or files. Headings inside a description
must be `###` or deeper.
```

Rules (a strict parser checks them; anything else is an error):
- `# Title` starts a group, `## Title` an item. Document order is priority.
- Number the items you create `{{first_id}}`, `{{second_id}}`, … in order. These are provisional labels so that `- depends:` lines can refer to them; PACE assigns the final IDs.
- Metadata lines go straight after the item heading, each at most once: `- depends: ID, ID` (other items in this roadmap, or existing items of the project if any are listed below), `- kind: <one lowercase word>` (use `debt` for known issues and tech debt, leave it out for normal feature work), `- parked` (someday items that shouldn't be scheduled yet).
- Descriptions should be self-contained enough to plan from, but link to the repo's docs rather than copying long passages.
{{existing}}
