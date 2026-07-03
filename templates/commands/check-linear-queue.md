---
description: >
  Polls Linear for lane-2 issues in Triage, picks the highest priority
  one, and runs the spec pipeline (specify → plan → tasks).
  Stops before implementation. Designed for autonomous polling via queue-loop.sh.
---

## Purpose

Autonomous spec worker. Checks Linear for work, picks up the next issue,
specs it out, creates a PR with the spec artifacts, and moves the issue
to Review for human approval. Does NOT implement.

## Workflow

### 1. Check the queue

Query Linear for issues matching ALL of:
- **Team**: Experiments
- **State**: Triage
- **Label**: lane-2
- **Project status**: "In Progress" (not Planned or Backlog)

Use `list_issues` with `team: "Experiments"`, `state: "Triage"`, `label: "lane-2"`.

Then for each result, fetch the project via `get_project` and **filter out** issues
whose project status is not "started" (i.e., skip Planned, Backlog, Paused projects).
Cache project statuses to avoid redundant lookups.

If **no qualifying issues found**: report "Queue empty, nothing to pick up." and **stop**.

### 2. Select the next issue

From the results, pick the single highest priority issue:
1. Sort by priority value (1=Urgent, 2=High, 3=Medium, 4=Low — lower number = higher priority)
2. If tied, pick the oldest (earliest `createdAt`)
3. Skip archived issues (`archivedAt` is not null)

Report which issue was selected and why.

### 3. Claim the issue

Move the issue to **"Spec"** state to prevent double-pickup.

Use `save_issue` with:
- `id`: the issue's UUID
- `state`: "Spec"

If the issue is already NOT in Triage when we try to fetch it,
someone else grabbed it — skip and try the next one.

### 4. Run /linear-to-spec

Invoke `/linear-to-spec` via the Skill tool, passing the issue identifier
(e.g. `EXP-75`) as the argument.

This will:
- Fetch the full issue details
- Run speckit-specify → speckit-plan → speckit-tasks
- Create Linear sub-issues from tasks.md
- Push the spec branch (no PR, no merge)
- Stop before implementation

Wait for it to complete. If it fails, report the error, move the issue
back to "Triage" state, and **stop**.

### 5. Update Linear status

`/linear-to-spec` already pushes the branch and posts a comment with the
branch name and artifacts. After it completes:

Move the issue to **"Review"** state via `save_issue`.

### 6. Report

Summarize:
- Which issue was picked up
- Spec artifacts generated
- Branch name (pushed, no PR — implementation will continue on this branch)
- Sub-issues created
- Issue moved to Review
- Whether the queue has more items

Then **stop**.

## What happens next (not this skill's job)

1. **Human reviews** the spec PR and the Linear issue
2. **Human moves** the issue to "Build" when satisfied
3. **`/check-implement-queue`** picks up "Build" issues and implements them
   (or human runs `/linear-implement EXP-XX` manually)

## Safety guardrails

- **One issue at a time**: only pick up one issue per invocation.
- **Don't fight over issues**: if an issue moved out of Triage, skip it.
- **Spec only**: do NOT run `/linear-implement` or `/speckit-implement`.
- **Clean git state**: always start from clean git state. If dirty, stop.
- **Branch isolation**: each issue gets its own branch. Never work on main.

## Error handling

- Queue empty → report and stop (normal).
- Issue already claimed → skip, try next issue in queue.
- Git state dirty → report and stop.
- linear-to-spec fails → move issue back to Triage, report, stop.
- Any unrecoverable error → report clearly what failed and stop.
