---
name: bureau
description: Operate a Bureau Linear ticket in the current Codex task. Use for spec, implementation, QA, review, status, or resuming an existing Bureau run.
---

# Bureau in the app

Work in this task using its selected model and tools. Read `scripts/bureau-stage.md` and `.bureau.json` in the adopting repository. Use the shared stage protocol; do not launch Claude, `codex exec`, tmux, a queue loop, or a shepherd to do the current task's coding work. Create separate sidebar tasks only when requested. Use specialist subagents only when the user requests delegation; otherwise conduct the review in this task.

## Choose the requested boundary

| Invocation | Work |
|---|---|
| `$bureau spec TEAM-123` | Specification through configured Spec Review |
| `$bureau implement TEAM-123` | Accepted tasks through QA/Build Review; incomplete work stays in Build |
| `$bureau qa TEAM-123` | Independent tests and permitted test additions |
| `$bureau review TEAM-123` | Review current HEAD, findings and verdict; stop before merge |
| `$bureau status` | Read-only local ownership/run status |
| `$bureau resume RUN_ID` | Inspect the saved context and continue preserved work |
| `$bureau TEAM-123 through review` | Continue authorized stages in order up to review |

Ordinary language equivalents work too. Infer scope from the user's request and prior authorization. Do not expand spec-only or review-only work into implementation. A full workflow request can continue through enabled optional stages and repair loops within its authorized boundary. Stop when a blocker needs missing information or authority, preserving completed work. A review request does not authorize posting comments to unrelated people or changing unrelated tickets.

## Operate a stage

1. Inspect `python3 scripts/bureau-runtime.py status`, current Git branch/diff, project guidance and the ticket. Discover available Linear tools by capability, not a fixed MCP server name. The shared state helper uses the configured API-key path; missing credentials are a setup blocker. Never print credentials.
2. Select the stage from configured state UUIDs. Inspect existing runs before preparing another. Attach the canonical issue branch first when it exists, preserving all work. Claim with `python3 scripts/bureau-runtime.py prepare ISSUE STAGE --owner OWNER`; use the current task ID when available, otherwise a descriptive owner. Keep the returned run ID. For an existing canonical branch, attach it without reset/clean or worktree detachment; if another checkout owns it, preserve both and use an authorized handoff.
3. Perform the stage directly. For spec, follow installed `.agents/skills/speckit-specify`, `speckit-plan`, and `speckit-tasks`; `$linear-to-spec` also explains sub-issue tracking. For implementation use the accepted tasks and `$linear-implement` procedure. When already prepared, those procedures reuse this run rather than preparing twice. QA/review follow the shared stage contract. Read actual code and run relevant checks; do not fabricate result evidence.
4. Commit work when within scope using `Bureau-Generated: true` trailers. Preserve actual authorship. Use a draft PR for incomplete work, and include test evidence and remaining blockers. Reuse an existing issue PR. Never reset the app checkout to make a stage pass.
5. Write result JSON to a unique temporary file outside tracked source, using the exact `run_id`, `stage`, current Git `head`, `outcome`, `summary`, artifact paths and actual test results. Review requires a verdict. Include the PR URL and blockers in the summary. Run `finish RUN --result FILE`. Do not also move the issue manually; the helper validates and transitions it.
6. Report changed files, test outcomes, remaining blockers and PR/artifact links. Open the relevant diff or artifact in the app when available. Continue to the next authorized stage using a new prepare; stop at the user's boundary. Default review completion leaves the PR open. Merge authorization must be explicit and still uses the complete existing merge gate.

A stale result or ownership conflict is a reconciliation step, not a reason to bypass checks. Follow [resume.md](references/resume.md). For local setup/actions follow [app-setup.md](references/app-setup.md).

For background status, bounded ticks, pause/resume, conflict scheduling and user-requested recurring supervision, read [supervision.md](references/supervision.md).
