---
description: >
  Polls Linear for lane-2 issues in Build state, picks one, and runs
  /linear-implement on it — executing ai-implementable tasks and stopping
  at needs-human tasks. Use after specs have been reviewed and approved.
---

## Purpose

Autonomous implementation worker. Picks up issues that have been specced
and approved (moved to "Build" by a human), runs `/linear-implement` to
execute ai-implementable tasks on the existing spec branch, and creates
a PR for human review.

## Workflow

### 1. Check the queue

Query Linear for issues matching ALL of:
- **Team**: Experiments
- **State**: Build
- **Label**: lane-2
- **Project status**: "In Progress" (not Planned or Backlog)

Use `list_issues` with `team: "Experiments"`, `state: "Build"`, `label: "lane-2"`.

Then for each result, fetch the project via `get_project` and **filter out** issues
whose project status is not "started" (i.e., skip Planned, Backlog, Paused projects).
Cache project statuses to avoid redundant lookups.

If **no qualifying issues found**: report "No issues ready for implementation." and **stop**.

### 2. Select the next issue

From the results, pick the single highest priority issue:
1. Sort by priority value (1=Urgent, 2=High, 3=Medium, 4=Low)
2. If tied, pick the oldest (earliest `createdAt`)
3. Skip archived issues

Report which issue was selected.

### 3. Run /linear-implement

Invoke `/linear-implement` via the Skill tool, passing the issue identifier.

This will:
- Check out the existing spec branch (created by `/linear-to-spec`)
- Find existing sub-issues (or create them if missing)
- Execute ai-implementable tasks in dependency order
- Stop at needs-human tasks
- Push and create a PR against main
- Move the parent issue to Review

### 4. Report

Summarize:
- Which issue was picked up
- Tasks completed vs needs-human vs blocked
- PR link
- Next steps for the human

Then **stop**.

## Safety guardrails

- **One issue at a time**: only pick up one issue per invocation.
- **Respect human gates**: never bypass needs-human flags. Stop and report.
- **Clean git state**: if dirty, stop.
- **Don't re-implement**: if sub-issues already exist and are Done, skip them.

## Error handling

- No Build issues → report and stop (normal).
- Spec branch not found → post comment on issue, stop.
- Task implementation fails → comment on sub-issue, set back to Todo, continue with next.
- Git conflicts → report and stop.
