# Resume a task or hand off work

Run `python3 scripts/bureau-runtime.py resume RUN_ID`. Compare its recorded workspace, stage, state and HEAD with current Linear/Git evidence. Read the preserved artifacts, diff, test outcomes and blocker summary.

For a prepared run in the same workspace with its claim intact and unchanged entry state, continue the unfinished stage using that run ID. Re-run checks invalidated by subsequent edits. Finish through the shared helper. A review whose HEAD changed needs a fresh review context; never attach an old APPROVE to new code.

A finished partial/blocked run has released ownership but retains progress. After resolving its blocker, prepare a new run for the same stage and continue from the existing branch/tasks. Do not recreate completed sub-issues or reset the branch. A successfully finished stage resumes at the next configured stage within the user's requested boundary.

For another workspace/owner, show its location and run ID. Stop a live background process before release. Use the app's supported handoff flow only when the user requests moving the task, or an explicitly authorized Git worktree handoff. Release the previous app lease only after inspecting its saved work and the owner's status. Then prepare in the destination checkout. A release authorizes neither reset nor deletion; adopting a worker checkout into an app task removes its disposable registration.

After interruption between remote state update and local finish, retry the identical result file. The helper recognizes the pending transition and avoids duplicate completion. A mismatched external state/configuration or changed result must be reconciled; do not edit stored records to force acceptance.
