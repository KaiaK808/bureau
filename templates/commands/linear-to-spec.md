---
description: >
  Fetches a Linear issue and runs the full spec-kit workflow up to
  (but not including) implementation. Use when given a Linear issue
  ID or URL and asked to spec it out.
---

## User Input

```text
$ARGUMENTS
```

You **MUST** have a Linear issue identifier (ID or URL) to proceed.
If `$ARGUMENTS` is empty, ask the user for one.

## Purpose

Automate the spec-kit specification pipeline from a Linear issue,
stopping before implementation.

## Workflow

### 1. Fetch the Linear issue

Use the Linear MCP tools to retrieve the full issue from the identifier
provided in `$ARGUMENTS`. Extract:
- Title, description, acceptance criteria
- Any linked sub-issues (via `parentId`) or parent epics for context
- Labels, priority, assignee (for metadata)
- Comments (may contain clarifications from the team)

If the issue has a `parentId`, also fetch the parent issue for context.
If the issue has sub-issues, list them.

Compile all of this into a structured requirements summary.

If the issue cannot be found, report the error and **stop**.

### 2. Check prerequisites

Before running speckit, verify that `.specify/memory/constitution.md` exists.
If it does not, tell the user to run `/speckit-constitution` first and **stop**.

### 3. Run speckit-specify

Invoke `/speckit-specify` via the Skill tool, passing the compiled requirements
as the argument. Format it as:

```
Feature name: <issue title>
Source: Linear issue <ID>

<compiled requirements from issue description,
 acceptance criteria, and relevant comments>
```

Wait for it to complete before proceeding.

### 4. Run speckit-plan

Invoke `/speckit-plan` via the Skill tool.

Use any tech stack information from:
- The project's constitution.md
- Labels or custom fields on the Linear issue
- Parent epic constraints

### 5. Run speckit-tasks

Invoke `/speckit-tasks` via the Skill tool to generate the task breakdown.

### 6. Create Linear sub-issues from tasks.md

Parse the generated `tasks.md` and create a sub-issue for each task under the
parent Linear issue.

For each task:
- **Title**: task name from tasks.md
- **Description**: full task spec including description, acceptance criteria,
  and file paths to create/modify
- **Parent**: set `parentId` to the parent issue's UUID
- **Team**: same team as parent
- **Project**: same project as parent
- **Priority**: inherit from parent, or 3 (Normal) by default
- **Blocking relationships**: use `blockedBy` to mirror dependency order from tasks.md

#### Triage labels

Ensure these labels exist on the team (create if missing):
- `ai-implementable` (color: `#22c55e` green)
- `needs-human` (color: `#f59e0b` amber)

Flag tasks as `needs-human` if they involve ANY of:
- Infrastructure or deployment changes (CI/CD, Docker, cloud config)
- Security-sensitive code (auth, encryption, secrets, permissions)
- External API integrations that require credentials or API keys
- Database migrations on production data
- Payment or billing logic
- Environment variables or secrets management
- Destructive operations (data deletion, schema drops)

Everything else gets `ai-implementable`. When in doubt, mark `needs-human`.

After creating all sub-issues, post a summary comment on the parent issue
listing all created sub-issues with their identifiers and labels.

### 7. Push branch and update Linear

Push the spec branch to remote so it survives worktree resets,
but do **NOT** create a PR or merge to main. The branch will be
reused by `/linear-implement` for the actual code changes.

1. Push the feature branch:
   ```
   git push -u origin HEAD
   ```

2. Post a comment on the Linear issue via `save_comment` with:
   - The **branch name** (so `/linear-implement` can find it later)
   - List of generated spec artifacts and their paths
   - Count of sub-issues created (ai-implementable vs needs-human)
   - Note: "Clarify was skipped — run `/speckit-clarify` during review if needed"
   - Note: "Spec branch pushed. Implementation will continue on this same branch."

### 8. Output summary

Provide a summary:
- Link to the Linear issue
- List of generated spec artifacts and their paths
- Sub-issues created (with identifiers and ai/human labels)
- Branch name pushed (no PR — branch stays open for implementation)
- Confirmation that the workflow stopped before implement

**Do NOT run speckit-implement. Do NOT create a PR. Explicitly stop here.**

## Error handling

- If the Linear issue cannot be found, report the error and stop.
- If speckit prerequisites are missing (no constitution.md),
  prompt the user to run `/speckit-constitution` first.
- If any speckit phase fails, report which phase failed and why.
- If sub-issue creation fails partway through, report which were created
  and which failed, then continue with the push step.
