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

Read `scripts/bureau-stage.md` and prepare the implementation stage with `python3 scripts/bureau-runtime.py prepare ISSUE implement`. Keep the run ID. Read the newest first-line `<!-- bureau-branch: BRANCH -->` marker from issue comments; use the shared `get_issue_branch` helper for legacy fallback. Do not guess branch names from team keys.

Inspect the current checkout and `git worktree list`. If the branch is held by another app/task, report its location and request a handoff only if one is not already authorized. Never reset, clean or detach a user checkout. Preserve existing changes; create/attach the issue branch only when compatible with the current workspace. Use `.repo.specs_dir` from `.bureau.json`.

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

For missing tasks, create sub-issues using the stable `<!-- bureau-task: PARENT-ID:T001 -->` marker described by `/linear-to-spec`:
- Ensure labels `ai-implementable` and `needs-human` exist on the team
- For each task from tasks.md, create a sub-issue (same format as `/linear-to-spec` step 6)
- Post a summary comment on the parent issue

Match existing sub-issues by stable marker, then by unique legacy task ID/title. Reuse matches, add missing markers, and report ambiguous matches without creating duplicates. Fetch every page before deciding a task is missing.

### 5. Execute ai-implementable tasks

Process tasks in dependency order. For each task marked `ai-implementable`:

1. **Check dependencies**: verify all `blockedBy` tasks are completed (the configured completed state).
   If not, skip and move to the next eligible task.

2. **Start**: update the Linear sub-issue status to the configured `build` UUID.

3. **Implement**: carry out the task according to its spec.
   - Follow the file paths and acceptance criteria exactly.
   - Use existing code patterns and conventions from the codebase.

4. **Test**: run any tests specified in the task. If no tests specified,
   at minimum verify the code compiles/lints.

5. **Commit**: create a git commit with message format:
   `{sub-issue identifier}: {task title}` with a `Bureau-Generated: true` trailer (matching the sub-issue identifier).

6. **Complete**: update the Linear sub-issue status to the configured `done` UUID.

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
   - Title: `{parent identifier}: {issue title}`
   - Body: summary of what was implemented, link to Linear issue,
     list of completed tasks, list of needs-human tasks still pending

3. Add the PR link to the Linear issue via `save_issue` with `links`.

### 8. Update Linear status

Write the shared result JSON using the prepared run ID, actual HEAD, completed artifacts and test evidence. Mark outcome `complete` only when all required tasks are done and checks pass; use `partial` or `blocked` otherwise. Keep incomplete work as a draft PR.

Run `python3 scripts/bureau-runtime.py finish RUN --result FILE`. It moves complete work to configured QA when enabled, otherwise Build Review. Partial/blocked work stays in Build. Do not mark incomplete work ready or move it into review manually. Include the PR link and remaining tasks in the summary. Do not merge.

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
