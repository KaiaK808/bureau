---
description: >
  Takes a completed speckit tasks.md, creates Linear sub-issues
  with dependency structure, then executes ai-implementable tasks
  in order. Use after linear-to-spec has completed.
---

## User Input

```text
$ARGUMENTS
```

You **MUST** have a Linear parent issue identifier (e.g. EXP-75) to proceed.
If `$ARGUMENTS` is empty, ask the user for one.

## Purpose

Hybrid implementation: create Linear sub-issues for visibility and tracking,
then execute ai-implementable tasks in dependency order — with human gates
on sensitive tasks.

## Workflow

### 1. Fetch the parent Linear issue

Use the Linear MCP tools to fetch the parent issue from `$ARGUMENTS`.
Get its:
- Issue ID (internal UUID)
- Team ID
- Project ID
- Existing labels
- Comments (look for the branch name posted by `/linear-to-spec`)

If the issue cannot be found, report the error and **stop**.

### 2. Check out the spec branch

Find the branch name from the Linear issue comments (posted by `/linear-to-spec`
during the spec phase). If not found, try the Linear `gitBranchName` field or
look for a remote branch matching `exp/*` + the issue identifier.

```
git fetch origin
git checkout <branch-name>
git pull
```

If no spec branch exists, report the error and tell the user to run
`/linear-to-spec` first and **stop**.

### 3. Parse tasks.md

Find the speckit tasks.md for the current feature. Look in `specs/*/tasks.md`
for the spec that matches the Linear issue.

Read the tasks.md and extract each task with its:
- Task number and name
- Description and acceptance criteria
- File paths to create/modify
- Dependencies (which tasks must complete first)
- Parallel flag `[P]` if present
- User story grouping

If no tasks.md is found, tell the user to run `/linear-to-spec` first and **stop**.

### 4. Find existing sub-issues

Sub-issues are created by `/linear-to-spec` during the spec phase.
List sub-issues of the parent issue using `list_issues` with `parentId`.

If **no sub-issues exist**, fall back to creating them:
- Ensure labels `ai-implementable` and `needs-human` exist on the team
- For each task from tasks.md, create a sub-issue (same format as `/linear-to-spec` step 6)
- Post a summary comment on the parent issue

If sub-issues **already exist**, match them to tasks.md entries by title.
Use the existing sub-issues — do not create duplicates.

### 5. Execute ai-implementable tasks

Process tasks in dependency order. For each task marked `ai-implementable`:

1. **Check dependencies**: verify all `blockedBy` tasks are completed (status = "Done").
   If not, skip and move to the next eligible task.

2. **Start**: update the Linear sub-issue status to "In Progress"
   via `save_issue` with `state: "In Progress"`.

3. **Implement**: carry out the task according to its spec.
   - Follow the file paths and acceptance criteria exactly.
   - Use existing code patterns and conventions from the codebase.

4. **Test**: run any tests specified in the task. If no tests specified,
   at minimum verify the code compiles/lints.

5. **Commit**: create a git commit with message format:
   `EXP-{number}: {task title}` (matching the sub-issue identifier).

6. **Complete**: update the Linear sub-issue status to "Done"
   via `save_issue` with `state: "Done"`.

7. **Next**: move to the next task in dependency order.

Tasks marked with `[P]` (parallel) that share no dependencies between
each other can be implemented in sequence — the parallel flag is for
human coordination, not agent parallelism.

### 6. Stop at human-flagged tasks

When encountering a `needs-human` task whose dependencies are all met:

1. **Do NOT implement it.**
2. Post a comment on the Linear sub-issue explaining:
   - Why this task was flagged (which triage rule triggered)
   - What context the human reviewer needs
   - What the expected outcome is
   - Which tasks are blocked waiting on this one
3. Update the sub-issue status to "Triage" or leave as "Todo".
4. Report to the user:
   - Which task is blocking
   - Why it needs human review
   - Which downstream tasks are waiting
5. **Pause and ask the user how to proceed.**

Do NOT skip human-flagged tasks or implement them without explicit approval.

### 7. Push and create PR

After all ai-implementable tasks are done (or blocked by needs-human tasks):

1. Push the branch:
   ```
   git push
   ```

2. Create a PR against main using `gh pr create`:
   - Title: `EXP-{number}: {issue title}`
   - Body: summary of what was implemented, link to Linear issue,
     list of completed tasks, list of needs-human tasks still pending

3. Add the PR link to the Linear issue via `save_issue` with `links`.

### 8. Update Linear status

Move the parent issue to **"Review"** state via `save_issue`.

Post a comment on the Linear issue:
- Link to the PR
- Summary: X tasks completed, Y needs-human, Z remaining
- Note: "Implementation complete. Review the PR and merge when satisfied."

### 9. Completion summary

Report:
- Total tasks: X
- Completed (ai-implemented): Y
- Blocked (needs-human): Z
- Remaining (waiting on dependencies): W
- List of commits created
- PR link
- List of needs-human tasks with their Linear links
- Next steps for the user

## Error handling

- If tasks.md is not found, tell the user to run `/linear-to-spec` first.
- If the spec branch cannot be found, report and stop.
- If the Linear parent issue cannot be found, report and stop.
- If a task implementation fails, update the sub-issue with a comment
  describing the failure, set status back to "Todo", and continue
  with the next independent task.
- If git operations fail (merge conflicts, etc.), report and pause.
