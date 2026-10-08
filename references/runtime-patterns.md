# Pipeline script patterns

The executable templates are authoritative; installation copies them instead of generating scripts from examples. Background stages load `bureau-config.sh`, preserve any caller stop boundary while loading trusted environment settings, run applicable Linear/provider preconditions and enter the issue/workspace ownership protocol before creative work. Use `run_stage_for` through quoted Bash arrays, as the current pipeline does; never construct or execute a provider command string. App stages use prepare/finish in the current checkout instead of worker resets.

**Queue picking: use `pipeline_pick_next`, not `claude -p`.** Every pipeline script that chooses the next issue from a state queue MUST call `pipeline_pick_next` from `bureau-config.sh`, which dispatches to `pick_issue` via the central registry. Do NOT generate the legacy pattern of spawning `$CLAUDE "You are a queue picker..."` — that pattern is unreliable in cron because headless Claude subprocesses can't complete OAuth for remote MCPs, and Linear's MCP OAuth tokens expire after ~1 hour. The first cron tick works, every subsequent tick fails silently with "No qualifying issues found."

Canonical picker block for each pipeline:

```bash
if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next <stage> issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")

  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi
```

Stage → registry mapping (defined once in `bureau-config.sh:pipeline_picker_args`; both `queue-loop.sh:preselect_issue` and each pipeline's own picker call read from there). Add a row to that function when adding a new pipeline; the table below is documentation, not the source of truth.

| Pipeline | State var | Required labels | Exclude labels |
|---|---|---|---|
| spec-pipeline.sh | `$BUREAU_STATE_TRIAGE` | `"$BUREAU_LABEL_LANE2_NAME"` | `""` |
| spec-review-pipeline.sh | `$BUREAU_STATE_SPEC_REVIEW` | `"$BUREAU_LABEL_LANE2_NAME"` | `""` |
| ux-pipeline.sh | `$BUREAU_STATE_DESIGN` | `"needs-ux,$BUREAU_LABEL_LANE2_NAME"` | `""` |
| copy-pipeline.sh | `$BUREAU_STATE_COPY` (opt-in) | `"$BUREAU_LABEL_NEEDS_COPY_NAME,$BUREAU_LABEL_LANE2_NAME"` | `"needs-human"` |
| implement-pipeline.sh | `$BUREAU_STATE_BUILD` | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human"` |
| qa-pipeline.sh | `$BUREAU_STATE_QA` (opt-in) | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human"` |
| code-review-pipeline.sh | `$BUREAU_STATE_BUILD_REVIEW` | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human"` |
| merge-pipeline.sh | `$BUREAU_STATE_MERGE` (opt-in) | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human,blocked,wip"` |
| rebase-pipeline.sh | `$BUREAU_STATE_MERGE` (opt-in) | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human,blocked,wip"` |

**Opt-in pipelines** (`copy`, `qa`) exit 2 (queue-empty) when their state UUID or required label name is empty in `.bureau.json`. This means a repo that doesn't configure them never sees them run — the pipeline script itself gates on the config, not just `agent_enabled` in `queue-loop.sh`. Both gates are defensive: the config gate catches the case where an operator enables the agent in `agents.qa: true` but forgot to configure the state.

**Merge policy gate.** Under `agents.merge_mode: "manual"`, `agent_enabled merge|rebase` is false (forced or not), so no dispatcher picks those stages, and `merge-pipeline.sh` and `rebase-pipeline.sh` check `bureau_merge_is_manual` right after sourcing the config and exit 2 — before `.env`, `precondition_linear`, the picker, gh or git. `code-review-pipeline.sh` checks `merge_mode_lacks_merge_state` at the same place and exits 24 when there is no Merge state to park an approval in. Keep these checks first; a policy that says "a human merges" must not depend on which caller started the stage.

`pick_issue` uses label **names** (not UUIDs) in its GraphQL filter, so custom labels like `ai-implementable`, `needs-human`, `needs-ux` work even when `.bureau.json` doesn't have their UUIDs captured. It reads the team/project/state config from the `$BUREAU_*` variables and does one direct GraphQL POST using `LINEAR_API_KEY` — no MCP, no OAuth, no Claude subprocess, no token expiry.

### All Linear glue uses direct GraphQL, never `$CLAUDE`

The same reasoning that kills `$CLAUDE` for queue picking kills it for every other Linear interaction. Headless `claude -p` subprocesses cannot refresh Linear's OAuth tokens, and a failing MCP call returns empty output that bash's `if [ -z "$X" ]` guards mistake for "no work". Every Linear glue operation must go through a helper in `bureau-config.sh`:

| Helper | Replaces |
|---|---|
| `move_issue <issue> <state-uuid>` | `$CLAUDE "Move Linear issue ... to state ..."` |
| `post_comment <issue> <body>` | `$CLAUDE "Post a comment ..."` |
| `get_issue_branch <issue>` | `$CLAUDE "Find the spec/implementation branch ..."` |
| `get_issue_comments <issue>` | `$CLAUDE "Check comments for code review feedback ..."` |
| `get_issue_detail <issue>` | per-script `linear_query` + `jq` for title/description/project |
| `get_issue_state <issue>` | per-script `linear_query` for the state-name check after picking |
| `add_issue_label <issue> <name>` | `$CLAUDE "Add label X to issue Y"` |

`$CLAUDE` is reserved for **creative work only**: spec-pipeline's optional pre-spec research call (WebFetch/WebSearch against current API docs, label-gated on `needs-research`) and its `speckit-specify`/`speckit-plan`/`speckit-tasks` invocations (read via the `.claude/skills/speckit-*/SKILL.md` files), implement-pipeline's main implementation call, code-review-pipeline's specialist review calls, spec-review-pipeline's validation prose. A rule of thumb: if the prompt is shorter than the response, it's glue — use a helper. If the prompt is longer and describes a task, Claude does the work.

### Deterministic branch discovery via bureau-branch marker

Linear's auto-generated `branchName` field is derived from the issue title and does **not** match the sequential spec-number branches the spec pipeline creates (`001-automated-tests`, `004-graphify-integration`, etc.). Before marker-based discovery, every "find the branch" lookup was a silent mismatch.

The fix is a marker comment that the **spec pipeline** posts along with the spec digest:

```markdown
<!-- bureau-branch: 001-automated-tests -->
**Spec Artifacts — TEAM-123**
...
```

The marker renders invisibly in Linear (HTML comment) but is parsed by `get_issue_branch()` on the way back. Every downstream pipeline (spec-review, ux, implement, code-review) resolves the branch from this marker, not from `branchName`. When the marker is missing or points at a branch that no longer exists, pipelines **fail loud** with stage-specific diagnostics, routing and a non-zero result. No silent fresh-from-main fallback, no 1300-line implementation runs on the wrong base.

### Validate against the appropriate base

Implementation and QA call `merge_origin_main_or_abort <issue> <stage-label>` after checking out their worker branch. With no third argument, the helper fetches and merges `origin/main`; it skips an already-contained base. It resolves only the supported trivial conflicts, aborts other conflicts and returns failure for stage-specific routing.

Code review instead reads the PR's actual `baseRefName` and matching head branch, validates both refs, fetches them explicitly and pins both commit SHAs. It passes the pinned base as the helper's third argument, which must be a commit SHA and is never refetched by the helper. The local validation merge supports the build check; all specialists review the immutable `BASE_SHA...HEAD_SHA` PR diff. This excludes parent-PR changes when the target is another feature branch.

Before posting or routing, review rechecks the PR identity/target and both authoritative remote tips. Changed or unavailable inputs preserve the review output and stop execution. A saved review boundary includes the base branch name and both remote SHAs, not the unpublished local merge commit alone. Retargeting or advancing either remote input reopens the boundary; older records without a base name retain their original `main` interpretation. `spec-review-pipeline.sh` does not perform this validation merge.

On a merge conflict, implementation, QA and code review return 17; the caller owns state/label routing. These local checks do not replace the independent CI/base gates at merge time.

### Per-stage model override

Each pipeline can run on a different model — different stages have meaningfully different requirements (spec/plan want strongest reasoning; implementation wants strongest coding; validators want a *different perspective* than workers; mechanical stages can use Haiku).

The mechanism is a single helper in `bureau-config.sh`:

```bash
run_stage_for <stage> [--append-system-prompt CONTEXT] PROMPT
```

Pipelines use quoted Bash arrays around `run_stage_for`, which passes file/stdin prompts to `bureau-provider.py`. The adapter selects Claude/Codex, resolves models per provider, validates final output, and preserves separate diagnostic evidence. `claude_cmd_for_stage` remains a legacy compatibility helper; do not execute its command string. See [provider runtime](../docs/provider-runtime.md) for exact precedence and permission behavior.

Configuration in `.bureau.json`:

```json
{
  "agents": {
    "model": "claude-sonnet-4-6",
    "spec":         { "model": "claude-opus-4-7" },
    "implement":    { "model": "claude-opus-4-7" },
    "code_review":  { "model": "claude-haiku-4-5-20251001" },
    "qa":           { "model": "claude-haiku-4-5-20251001" }
  }
}
```

Stage slots include `spec`, `spec_review`, `ux`, `copy`, `implement`, `qa`, `code_review`, `merge`, `research` and `upstream_port`. Preserve legacy Claude model ownership; use provider-specific settings for Codex under v1 compatibility. See [background operations](operations.md) for the supported runtime overrides.

Each background invocation starts a separate provider process. Share explicit artifacts and the validated stage result; do not assume conversation or hidden reasoning transfers between providers.

**Validator independence.** The strongest reason to mix models is that a validator using a *different model* from the worker catches what the worker missed. Bureau's code-review pipeline already runs 3 specialists in parallel for orthogonal *perspectives*; setting `agents.code_review.model` to a different model than `agents.implement.model` adds a *model-level* difference on top of the role-level one.

### Auto-restart supervisor for queue-loop

`queue-loop.sh` runs an infinite while-true loop — any exit means the process was killed (OOM, terminal disconnect, unhandled bash error, panicked subprocess). Without a supervisor the dead tmux pane stays dead until a human notices.

**`scripts/queue-loop-supervised.sh`** is a drop-in wrapper that `start-bureau-v2.sh` uses in place of `queue-loop.sh`. It:

1. **Restarts on crash** with exponential backoff: 10 s → 30 s → 60 s → 300 s (capped at 5 min).
2. **Resets the crash counter** after `BUREAU_SUPERVISOR_STABILITY_WINDOW` seconds of clean runtime (default 1 h). A long-lived agent that crashes once doesn't permanently cap its restart speed.
3. **Gives up** after `BUREAU_SUPERVISOR_MAX_CRASHES` consecutive crashes (default 5) and fires a Telegram alert with the tail of the queue log before exiting 1.
4. **Stops on SIGINT, SIGTERM and SIGHUP** (SIGHUP since v3.2): it sends SIGTERM to the queue loop and exits 0 without triggering the restart logic. `Ctrl+C` in the tmux pane, or closing the pane, signals the whole pane group, the runtime in front of the stage in flight included, so everything stops cleanly; a signal to the supervisor's process alone reaches only the queue loop, and the stage in flight runs to its end (docs/troubleshooting.md, "How to stop a run").

Logs to `logs/supervisor-<mode>.log`. The Telegram alert uses the standard `alert_telegram` helper, so it's a no-op when credentials are unset.

Configuration in `.bureau.json`:

```json
{
  "supervisor": {
    "max_crashes": 5,
    "stability_window": 3600
  }
}
```

`start-bureau-v2.sh`'s `add_agent_window` function calls `./scripts/queue-loop-supervised.sh $mode $INTERVAL` instead of `queue-loop.sh` directly. To opt out of supervision (e.g. for debugging), call `queue-loop.sh` directly from a bench pane.

### Drain before refilling — single-flight + stage-priority

In active repos with many concurrent feature tickets, branches accumulate divergence faster than the pipeline can drain them. Cause: every cron tick, multiple branches re-run `merge_origin_main_or_abort` against an `origin/main` that advanced since last tick. Trivial conflicts auto-resolve, but real conflicts pile up and the loop spins. Two complementary knobs:

**A. Stage-priority sort in `queue-loop.sh`'s `all` mode.** Fan-out order is *reversed* from state-machine sequence — `merge`/`rebase` first, `spec` last. When multiple stages have pickable issues, attention goes to the ones closest to Done. Drains before refilling. Zero behaviour change for the default deployment (each agent runs in its own tmux window with its own `queue-loop.sh <mode>` — independent, so cross-stage ordering only affects the single-process `all` mode).

**B. `BUREAU_MAX_CONCURRENT_ISSUES` cap.** Repo-wide cap on how many *distinct* issues the bureau works on simultaneously. Default `0` = unlimited (current behaviour). Set to `1` for single-flight mode (drain one issue end-to-end before another enters Spec). Implementation:

- `count_in_flight_issues()` in `bureau-config.sh` queries Linear for issues in any state between Spec (inclusive) and Done (exclusive). Issues with parking labels (`needs-human`, `blocked`, `wip`) are excluded so a stalled issue doesn't deadlock the cap.
- `spec-pipeline.sh` checks the count immediately after preconditions; exits 2 (queue-empty) if at cap. Only spec gates — downstream stages keep running on already-in-flight issues, so cap=1 still drains the current ticket.
- Fail-open on Linear errors: `count_in_flight_issues` returns "0" on any query failure, so a network blip doesn't block work.

Configuration in `.bureau.json`:

```json
{
  "agents": {
    "max_concurrent_issues": 1
  }
}
```

The cap is a Linear query, not an atomic cross-repository admission lock. Keep issue/workspace ownership and intentionally separated project scopes; do not treat single-flight configuration as distributed coordination.

### Fail-loud observability

Every pipeline script:

1. Starts with `set -euo pipefail`.
2. Calls `precondition_linear` before any Claude work, exiting 10 if `LINEAR_API_KEY` is missing or invalid (one-line `viewer { id }` query).
3. Uses distinct exit codes for each failure class so `queue-loop.sh` can alert correctly:

| Exit | Class | Meaning |
|---|---|---|
| 0 | ok | completed successfully |
| 2 | queue-empty | nothing to pick — normal |
| 10 | linear-down | `LINEAR_API_KEY` missing/invalid |
| 11 | worktree-dirty | uncommitted changes in worktree |
| 12 | no-branch | bureau-branch marker missing or points at a non-existent branch |
| 13 | no-tasks | tasks.md expected but missing |
| 14 | build-failed | build precondition failed; upstream-port build command failed |
| 15 | no-pr | PR expected but not found; also upstream-port test-command failure |
| 16 | provider-unauth | selected provider CLI not authenticated |
| 17 | rebase-needed | merge/apply conflict, including upstream-port |
| 18 | gh-failed | GitHub/publication/preflight failure; review also uses it for invalid or changed PR inputs |
| 19 | rebase-rejected | force-with-lease push refused |
| 20 | stopped-before-merge | review boundary reached |
| 21 | ownership-conflict | checkout/issue ownership refused |
| 22 | provider-or-result-error | provider execution or result validation failed |
| 23 | quota-wait | provider quota requires waiting |
| 24 | environment-blocked | required execution environment unavailable (includes npm dependencies that could not be restored) |
| 25 | needs-human-or-paused | human attention or dispatch pause |
| 26 | cancelled-ticket | ticket is cancelled |
| 27 | linear-unusable | a Linear answer stayed unusable after every retry |
| 124 | timeout | bounded execution timed out |
| 130 | cancelled-run | invocation cancelled |

Other codes map to `error-N`; inspect the diagnostic rather than inferring completion. [Exit codes](../docs/exit-codes.md) and `bureau-config.sh:exit_class` define the current protocol.

`queue-loop.sh` captures the exit code, maps it to a class, and calls `alert_telegram` (throttled to max 1 alert per issue/pipeline/exit code per hour and repository via `<git common dir>/bureau/alert-throttle.log`; the alert names the repository). The alerter is a best-effort no-op when `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALERT_CHAT_ID` are unset, so dev environments don't break.

### Spec pipeline failure recovery

`spec-pipeline.sh` moves the issue from Triage → Spec **before** running speckit phases. To prevent stranding issues in Spec on failure:

1. **Preconditions before state mutation**: `precondition_linear` (exit 10) and `precondition_runner spec` (exit 16) run before the Spec move. Provider authentication uses the CLI authentication-status command; it does not spend a creative model call to test login.
2. **Recovery trap**: An EXIT trap (`_spec_recovery`) is installed immediately after the Triage→Spec move. On any non-zero exit during speckit phases (specify, plan, tasks, crosscheck, push), the trap routes the issue back to Triage with an explanatory comment and fires a Telegram alert.
3. **Trap cleared on success**: The trap is cleared (`trap - EXIT`) just before the final `move_issue → Spec Review`, so the success path doesn't trigger recovery.

This pattern ensures issues are never stranded in Spec with zero artifacts. The same recovery approach can be extended to other pipelines.

### Pre-spec research (optional, label-gated, best-effort)

For issues that integrate with external APIs/SDKs or pin to specific library versions, the spec agent's stale training data is a known failure mode — it confidently invents method signatures and config keys that don't exist. `spec-pipeline.sh` solves this with an **optional** research pass that runs immediately before `speckit-specify`, gated by:

1. The Linear label `needs-research` is present on the issue, **and**
2. `.agents.research` is configured in `.bureau.json` (either `true` or `{"model": "<id>"}` — typically a cheaper model like Haiku since the work is reading docs, not designing code).

When both conditions hold, `$CLAUDE` is invoked with WebFetch/WebSearch enabled and instructed to compile a markdown digest of the relevant APIs (current versions, endpoints/method signatures, recent breaking changes, gotchas). The output must start with the marker `<!-- bureau-research: <api-list> -->`. On success the digest is posted to Linear as a comment (visible to spec-review for traceability), injected into the `speckit-specify` prompt as `$RESEARCH_CONTEXT` (the spec agent is told to treat it as authoritative for API shapes), and the `needs-research` label is stripped.

The whole stage is **best-effort**: a failed research call (non-zero exit, missing marker, network down) is swallowed with `|| true` / `|| echo ""` and the pipeline falls through to specify without research context. The exit-code protocol (0/2/10–16) is untouched — no new class. The label is only stripped *after* a successful `post_comment`, so a crashed run leaves the label in place and the next pick retries naturally. Default is **off** (no `.agents.research` entry → `agent_enabled` returns false), so existing deployments are unchanged until opted in.

### Worktree ownership

Background entry points claim issue and checkout ownership through `bureau-runtime.py`, then use `bureau-worker.sh` for a registered disposable checkout. Reset refuses unregistered directories and requires a matching live claim; the one exception is a preserved linked worktree of this repository that a non-spec stage can reset without loss (no uncommitted or untracked changes, HEAD on the fetched `origin/<branch>`), which it registers again with one line on stderr. The worker releases its own branch when finished. A branch held by an app/user checkout causes an ownership conflict, never an automatic detach. App work uses prepare/finish and preserves the current tree.

### JSON-block parsing for stage outputs

Every pipeline whose output the shell parses — `spec-review`, `implement`, `qa`, `copy`, `code-review` — now instructs Claude to emit a trailing fenced `` ```json `` block with the structured fields the shell needs (verdict, status, ui_work_needed, strings_changed, etc.). The shell side uses `parse_claude_json` from `bureau-config.sh`, which `awk`-extracts the last `json` block and pipes it to `jq -r`. **Never** parse these outputs with `grep` / `sed` on the prose — the merger format drifts as the model varies (this drift is why commit `2397837` added the `VERDICT=${VERDICT:-BLOCK}` fallback in code-review). A regex parse that silently lands on "empty" and routes the happy-path without anyone noticing is worse than a loud parse failure.

The review merger is the one call with a schema (`bureau-review.schema.json`). A provider that enforces a schema returns only the object, so the merger's prose travels inside it: a required `comment` (markdown: specialist summaries, findings by class with file:line, fixes needed) and a required `findings` list of `{file, line, class, msg}`. The stage posts the comment and then the rest of the object as the fenced verdict, on the PR and in the Linear REQUEST_CHANGES and BLOCK comments, and reads the verdict from the object, never from the comment. Comments over 60,000 bytes are shortened in the middle (`bureau_cap_comment`): Linear and GitHub refuse longer ones.

When adding a new Claude call whose output needs programmatic consumption:
1. End the prompt with a fenced `` ```json ``` block showing the exact schema as a template (zeros / empty strings / empty arrays for defaults).
2. In the calling shell, use `parse_claude_json "$OUTPUT" '.field'`.
3. Add an explicit fallback for empty parses — the safer choice (FAIL for reviewers, BLOCK for verdicts, NEEDS_HUMAN for executors) so a malformed response doesn't auto-advance a state.

### New agents: QA (mechanical) and Copy (opt-in)

- **`qa-pipeline.sh`** slots between Build and Build Review. It's a *mechanical* executor — runs `npm test` / `cargo test` / `pytest` etc. before any Claude call, only engages Claude on test failure or missing coverage. Its job is distinct from code-review: QA confirms the test oracle passes; code-review judges the code's correctness in paths tests don't cover. Running QA first also keeps code-review from burning tokens on a PR whose build is red. Opt-in via `states.qa` + `agents.qa: true`. When QA is configured, `implement-pipeline.sh` routes issues to QA first instead of directly to Build Review.

- **`copy-pipeline.sh`** slots between Design and Build (or wherever you route `needs-copy` issues). It's opt-in at both the state level (`states.copy`) and the label level (`labels.needs_copy.name`). It reads an optional voice guide (`repo.copy_voice_file`) and polishes user-facing strings — button labels, error messages, empty states. It leaves tests, logs, and code comments alone.

Both pipelines use the same JSON-block output convention and the same exit-code vocabulary as the other stages (10 / 11 / 12 / 16 for preconditions, 2 for queue-empty, 0 for success).

### New agents: Merge (gated) and Rebase (opt-in, force-pushes)

Reviewed PRs accumulate when nothing closes the loop: mergeable-and-approved PRs sit waiting for someone to click Merge, branches go DIRTY when main moves, and the 3-cycle loop-breaker escalates to needs-human without followup. The `merge` agent gates and merges; the `rebase` agent unsticks DIRTY bureau-only branches.

- **`merge-pipeline.sh`** picks issues from `BUREAU_STATE_MERGE` and merges only when EVERY gate passes:
  1. PR `state == OPEN`
  2. `mergeStateStatus == CLEAN` (GitHub heuristic — async-cached, lax when branch protection isn't strict)
  3. **`pr_ci_is_green` (bureau-enforced, NRSR)**: every check-run AND legacy status context on the PR's current head SHA is `completed` and `success|skipped|neutral`. Pending/in-progress is rejected. Independent of `mergeStateStatus` because CLEAN passes when no required-checks rule is configured. Toggle: `.agents.merge_require_green_ci` (default true; only the JSON `false` switches it off, any other value keeps it with a warning). A head with no check run and no status at all is `not yet` until its commit is older than `.agents.merge_ci_start_grace_seconds` (default 1800), blocked after. A check run still `queued` (taken by no runner) is `not yet` until its `started_at` is older than `.agents.merge_ci_queued_grace_seconds` (default 3600), blocked after with its own line (`ci: check <name> queued for N s on … — runner offline?`).
  4. **`pr_base_is_current` (bureau-enforced, NRSR)**: PR's `baseRefOid` == `origin/<baseRef>`'s HEAD. Catches the stale-base race where mergeStateStatus's async cache still reads CLEAN after main moved. Toggle: `.agents.merge_require_up_to_date` (default true; only the JSON `false` switches it off).
  5. Latest PR comment matching `## Code Review v2 — ` carries `**Verdict**: APPROVE` (or `AUTO_APPROVE`)
  6. No `needs-human` / `blocked` / `wip` label on the PR
  7. Zero unresolved review threads (GraphQL `pullRequest.reviewThreads`)

  The entire gate set is **re-evaluated just-in-time** (a second `evaluate_merge_gates` call) immediately before `gh pr merge`. If anything regressed between the initial pass and the merge call (most importantly: gate 4 because a prior tick may have merged a different PR that advanced main), the pipeline aborts with `exit 0` and the next tick re-evaluates. This closes the window where mergeStateStatus's async cache could let a stale-base PR slip through.

  Eligible: `gh pr merge N --squash --match-head-commit <head the just-in-time gate checked>` (a push in between makes GitHub refuse it; the stage then ends with `2`) with a sanitised `--subject`/`--body` from `merge-body.sh`, so no CI suppressor from the commit list reaches `main` (rebase merges plain, gh takes no body for it; no `--delete-branch`, no `--auto` — see `code-review-pipeline.sh:314-322` for the worktree/detached-HEAD rationale; `--auto` would queue the merge for later and silence loud failures). On success: post Linear comment, move issue to Done.

  Not eligible: comment on the PR with the precise blocker, but **only if blockers changed** since the bot's last `Bureau merge gate` comment (sorted-line diff). This makes the script safe to run every poll interval without comment spam. Then exit `2` when the gate is not yet decided (pending or not started checks, GitHub still computing, a failed gate read, a hold label on the PR, conflicts the rebase stage resolves) and `25` when it is decided against the merge; `BUREAU_MERGE_GATE_REPORT` receives the outcome and the gate lines. The review stage's inline merge ends with the same codes, and the review acts on them: Done after a merge; on `2` it records the APPROVE (`bureau-supervision.py stop --merge-gate-wait`), comments once and ends with `2`, and the next pick reuses the approval without a model review or new comments and runs the gate again (the build check only when it did not pass for the same head, base and command); the queue's review picker puts such a ticket after every other candidate while its head is unchanged (and still takes it when nothing else can be picked), for two poll intervals (at least 300 s) after the first "not yet", doubling up to `.agents.merge_gate_recheck_seconds` (default 3600; `merge_gate_waits` in `bureau-config.sh`, `bureau-supervision.py gate-waits`); the merge stage marks its own `2` and `25` (`bureau-supervision.py merge-wait`), and the merge picker holds those tickets the same way; on `25` it records the APPROVE too (reused once a human clears the blocker and removes the label), sets `needs-human`, comments the gate lines and ends with `25`. In a disposable worker it first resets its worktree to the reviewed head, so the worker does not keep the local validation merge as unfinished work. `--dry-run` keeps `0`.

  `--dry-run` prints gate verdicts and the action without mutating anything — use to audit before trusting it.

- **`rebase-pipeline.sh`** is OFF by default because it force-pushes (mutates shared remote state). Picks from the same `BUREAU_STATE_MERGE` pool and only fires when:
  1. `mergeStateStatus == DIRTY` (real merge conflict; BEHIND/UNSTABLE explicitly NOT handled — squash-merge tolerates BEHIND, and adding BEHIND would expand the force-push surface unnecessarily)
  2. Every commit ahead of `origin/main` carries a `Co-authored-by: ...Claude...` trailer. Any human commit in the divergence → skip with explanatory comment. Humans rebase their own branches.

  Rebase succeeds → `git push --force-with-lease` → move issue back to Build Review (re-trigger code-review against the new base by state move, NOT by gaming `pick_issue` with marker comments). Conflict on rebase → `git rebase --abort` + label `needs-human` + comment.

  `--dry-run` prints the gates and the intended action without rebasing or pushing.

**Routing in `code-review-pipeline.sh`**: the APPROVE branch follows `agents.merge_mode`. With `manual` a human merges: code-review moves the issue to the Merge state and posts that the PR awaits a manual merge; the merge and rebase stages refuse there. With `auto` (the default) and `agents.merge: true` AND `BUREAU_STATE_MERGE` set, code-review moves the issue to Merge and posts "awaiting merge gate" — the merge agent takes over. With `auto` and the merge agent off, code-review runs `merge-pipeline.sh` inline (`BUREAU_INLINE_MERGE=1`): the same gate set and just-in-time checks as the merge agent, then the merge and Done. A requested stop (`--no-merge`) ends before either automatic route.

**Important:** opting in to merge does NOT auto-approve anything. Code review still has to pass the 3-cycle loop-breaker first. The 3-cycle escalation to needs-human is a deliberate protection, not something to optimize around. Two human eyes on a first APPROVE remains cheap and is not a goal of this pipeline.

### Bounded retry loop in implement-pipeline.sh

`implement-pipeline.sh` runs the selected provider inside a `for (( i=1; i<=MAX_ITER; i++ ))` loop instead of a single call. Each iteration: invoke `$CLAUDE`, parse the strict JSON status block via `parse_claude_json`, run the squash-range check (a CI suppressor in a commit message, or a range that cannot be read, stops the loop as `CI_MARKER`), push whatever was committed (deferred to the end-of-run push while a PR is open when `agents.implement.push_each_iteration` is `false`), decide whether to continue. A pass whose provider call fails, or a timed-out one when no pass is left, ends the stage after pushing every commit origin lacks, in both push modes (`push_if_ahead`); every other way out before the end-of-run push (a signal, a crash, a failed `/goal` run) does the same from the EXIT trap. The pipeline previously emitted that JSON contract but never read it back — every exit-0 run shipped to Build Review even on `status: PARTIAL`, leaking half-done work into review.

Knobs (env-tunable, all have safe defaults):

```bash
BUREAU_IMPL_MAX_ITER=3          # max Claude passes per tick
BUREAU_IMPL_ITER_TIMEOUT=1800   # per-iter wall-time cap (seconds)
BUREAU_IMPL_TOTAL_TIMEOUT=5400  # cumulative wall-time cap (seconds)
```

Defaults give ≤90 min worst case per tick before the issue is parked. Per-iter timeout uses `timeout` (Linux) or `gtimeout` (macOS via `brew install coreutils`); if neither is on PATH the cap degrades to cumulative-only with a WARN.

Loop invariants:

- **Preserve every iteration.** The executor commits Codex changes and publishes progress when permitted. A failed worker with dirty or unpublished work loses its disposable registration; inspect/resume it before another reset.
- **Single-strike stuck detector.** `tasks_done == 0 AND fixed_review_items == [] AND COMMITS_THIS_ITER == 0` ⇒ park the issue. Commit count is the load-bearing signal — it catches the "spent the iter debugging without marking [X]" case the tasks.md hash alone would miss.
- **Re-fetch review feedback per iteration.** Humans may add `Code Review … Changes Requested` comments mid-run.
- **No routing `trap ... EXIT`.** A hard crash bails via `set -e` and queue-loop sees the non-zero exit; a trap that routed the issue away from Build on crash would be the opposite of what's wanted. The only EXIT trap (`_push_on_exit`, both push modes since v3.2) pushes whatever origin lacks, keeps the exit code and touches nothing in Linear.

Terminal status routing:

For Codex implement turns only, a NEEDS_HUMAN result whose non-empty `notes.needs_human` array carries only `SANDBOX_GATE:` reasons can become COMPLETE before the post-implement hook when a test command and commits beyond `origin/main` exist. The configured tests run once after the final push outside the Codex sandbox; red restores NEEDS_HUMAN, and an intervening hook or squash-range failure still halts. The strict result reader and full contract are in [Background providers](../docs/provider-runtime.md#codex-implementation-sandbox-gate).

| Status | PR | needs-human label | State move | escalations.log |
|---|---|---|---|---|
| `COMPLETE` | flipped to ready | no | → QA or Build Review | no |
| `NEEDS_HUMAN` / `STUCK` / `CAP_TIME` / `PARTIAL` | draft (visible to reviewers) | yes | none (stays in Build) | yes |
| `CI_MARKER` (squash-range check not clean; overrides every other status) | draft, report posted on the PR | yes | none (stays in Build) | yes |

PRs open as `--draft` during intermediate iterations so QA and code-review don't trigger on half-done work; flipped to ready via `gh pr ready` only on COMPLETE.

### Escalation log: `log_escalation` helper

Every `needs-human` escalation appends one tab-separated line to `logs/escalations.log` AND fires a JSONL `emit_event "event=escalation"` to `logs/events.jsonl`. Two sinks: the TSV file is regex-matchable for operator monitors (`tail -F | grep`), the JSONL firehose stays queryable by `/bureau-learnings`.

Line format (verbatim, tab-separated):

```
2000-01-01T00:00:00Z<TAB>ESCALATED<TAB>TEAM-123<TAB>code-review<TAB>cycle=3<TAB>reason="REQUEST_CHANGES exceeded max_review_cycles"<TAB>pr=56<TAB>branch=049-parliament-debate
```

Required regex (used by the test suite and external monitors):

```
^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+ESCALATED\s+([A-Z]+-\d+)\s+(\S+)\s+cycle=(\d+)\s+reason="([^"]+)"\s+pr=(\d+)\s+branch=(\S+)$
```

Hooked at every site where a pipeline labels `needs-human`:

| Site | Pipeline | Triggering condition |
|---|---|---|
| Cycle-limit + BLOCK fallthrough | code-review | `REVIEW_CYCLE_COUNT >= MAX_REVIEW_CYCLES` |
| gh-merge-fail-after-approve | code-review | `gh pr merge` non-zero or PR state != MERGED |
| BLOCK verdict | code-review | reviewer returned BLOCK (not cycle-limit) |
| NEEDS_HUMAN verdict | qa | qa flagged out-of-scope failure |
| Retry-loop terminal | implement | NEEDS_HUMAN / STUCK / CAP_TIME / PARTIAL / CI_MARKER |

Logs **only** on `add_issue_label` success — the call site uses `if add_issue_label … then log_escalation … fi`, so a Linear API hiccup doesn't produce a phantom escalation. Embedded double quotes in the reason text are scrubbed to single quotes to keep the line regex-matchable.

`bureau-config.sh` ships via `--resync-scripts`; existing target repos pick up `log_escalation` on the next resync without special wiring. Add `logs/escalations.log` to the same `.gitignore` template entry that already excludes `logs/queue-*.log`.

### Shared prompt grounding: `build_spec_context`

Every pipeline that invokes Claude for non-trivial work now calls `build_spec_context "$SPEC_DIR"` and injects the result into the prompt. The helper enumerates `SPEC.md`, `CLAUDE.md`, and the per-ticket artifacts (`spec.md`, `plan.md`, `research.md`, `tasks.md`, `design.md`) that exist, and appends the "pinned decisions win; cite the pin when declining" disciplinary paragraph. Originally only `code-review-pipeline.sh` had this grounding; spreading it fixed re-surfacing of deferred findings in spec-review and implement, and reduced REQUEST_CHANGES cycles where an implementer re-introduced a pattern code-review had already declined.

---


These describe the background templates. The scripts are authoritative; do not regenerate them from these examples.


### Interrupted worker recovery

A cancelled background run keeps its issue/workspace leases and revokes disposable-worker registration. An exited shell does not prove that nested stage processes stopped. Inspect saved work and live processes before `bureau-runtime.py release RUN`; release refuses known live writers or unavailable process inspection. The checkout stays unregistered after release, ready for explicit app adoption or retirement: the interrupt message names the release command, the worktree and its unpushed local branch, and a rerun that meets the claim or the unregistered worktree stops with 21, sets `needs-human` and comments the same steps once. Never reset it automatically to recover from cancellation.
