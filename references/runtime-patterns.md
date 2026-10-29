# Pipeline script patterns

The executable templates are authoritative; installation copies them instead of generating scripts from examples. Background stages load `bureau-config.sh`, preserve any caller stop boundary while loading trusted environment settings, run applicable Linear/provider preconditions and enter the issue/workspace ownership protocol before creative work. Use `run_stage_for` through quoted Bash arrays, as the current pipeline does; never construct or execute a provider command string. App stages use prepare/finish in the current checkout instead of worker resets.

Environment snapshots use the absolute Python interpreter resolved at `bureau-env.sh` load from `/usr/bin/getconf PATH`, with the command hash cleared in the resolving subshell. Do not replace that invocation with a PATH lookup: unexported keys travel to the helper on stdin. Capture the child-visible exported environment before command substitution: Bash 5.2 refreshes exported `SHELLOPTS` there and can hide inherited values behind caller locals. A process-substitution pipe preserves those values; Bash 3.2 in POSIX mode retains the inline capture. Missing trusted Python or a failed snapshot exits with 24 before the child runs. The short-secret exception requires matching actual CODESET names or resolved locale identities; a matching character-probe length is insufficient. Unknown effective Bash locales (including empty declared locale bindings) or different encodings match every nonempty secret anywhere as bytes, regardless of length. Proven equivalence retains Bash's visible-secret lengths and libc/UTF-8 counting for hidden exports; CODESET names are never used as Python codecs. See [configuration](../docs/configuration.md#numeric-runtime-settings) for dependencies and filtering rules.

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

Implementation and QA call `merge_origin_main_or_abort <issue> <stage-label>` after checking out their worker branch. With no third argument, the helper fetches and merges `origin/main`; it skips an already-contained base. It resolves legacy and configured deterministic conflicts, aborts other conflicts and returns failure for stage-specific routing.

`bureau-conflicts.py` owns the merge transaction. Optional `repo.conflict_resolution` permits verified insertion-only unions and regeneration over merged sources, with an explicit command beyond whitespace/comments in `repo.test_command` outside the provider sandbox. Shared nonblank insertion anchors and new ATX/Setext headings decline conservatively. After source resolution, `restore_worktree_deps` prepares dependencies from the merged manifests before any generator/check; generators may then change only their declared outputs. Failure restores changed snapshot paths and any required parent access, preserves unchanged ignored paths and restores saved directory modes after the Git/index reset. Filesystem errors on one path do not stop other restores or the reset attempt; incomplete restoration declines. Existing exit-17 routing remains. A green repair grants no review approval or reusable gate. See [configuration](../docs/configuration.md#deterministic-conflict-resolution) and [stage protocol](../docs/stage-protocol.md#deterministic-conflict-resolution) for exact rules, snapshot costs and preservation limits. Keep shared generated/changelog paths in overlap admission.

Opt-in `agents.conflict_resolver` extends that transaction through `run_stage_for conflict_resolver` for remaining regular text source conflicts. Actual local and fetched remote divergence must both be Bureau-only against the pinned base. Fsynced ticket/base reservations in the common Git directory survive evidence pruning, HEAD changes and worktree recreation. The executor verifies files/index, regenerates outputs and runs explicit external tests after the entire merge/rebase; failure rolls back the whole operation, holds the ticket and returns 17, with withdrawal/silence/control exits preserved. Semantic review merges publish normally and re-pin inputs before specialists/reuse. Only rebase uses a force-with-lease pinned to the fetched remote tip. Green repair grants no review or gate reuse. See [model conflict resolution](../docs/stage-protocol.md#model-conflict-resolution).


Code review instead reads the PR's actual `baseRefName` and matching head branch, validates both refs, fetches them explicitly and pins both commit SHAs. It passes the pinned base as the helper's third argument, which must be a commit SHA and is never refetched by the helper. The local validation merge supports the build check; all specialists review the immutable `BASE_SHA...HEAD_SHA` PR diff. This excludes parent-PR changes when the target is another feature branch.

Before posting or routing, review rechecks the PR identity/target and both authoritative remote tips. Changed or unavailable inputs preserve the review output and stop execution. A saved review boundary includes the base branch name and both remote SHAs, not the unpublished local merge commit alone. Retargeting or advancing either remote input reopens the boundary; older records without a base name retain their original `main` interpretation. `spec-review-pipeline.sh` does not perform this validation merge.

Approval reuse additionally requires the saved policy version, severity floor, cycle allowance and total-round limit to match the current review policy. A policy change or a legacy approval without policy metadata requires a fresh review once the requested stop is cleared; an active stop remains stopped. Gate polls use the same policy binding. Required fixes can be deferred only through exact nonblank finding-message matches; see [review convergence](../docs/stage-protocol.md#review-convergence) for round-marker ownership and the remaining verdict protections.

On an active ticket's merge conflict, implementation, QA and code review return 17; the caller owns state/label routing. The trusted PR 9 checkpoint runs before the merge transaction and between its dependency/generator/test steps, including before the repaired merge commit. Confirmed withdrawal rolls the transaction back before the shell consumes the shared latch and exits 0/25/26. A failed repair refreshes ticket state before conflict routing even when its comment is throttled; a running command finishes its current step. These local checks do not replace the independent CI/base gates at merge time.

### Per-stage model override

Each pipeline can run on a different model — different stages have meaningfully different requirements (spec/plan want strongest reasoning; implementation wants strongest coding; validators want a *different perspective* than workers; mechanical stages can use Haiku).

The mechanism is a single helper in `bureau-config.sh`:

```bash
run_stage_for <stage> [--append-system-prompt CONTEXT] PROMPT
```

Pipelines use quoted Bash arrays around `run_stage_for`, which passes file/stdin prompts to `bureau-provider.py`. The adapter selects Claude/Codex, resolves models per provider, validates final output, and preserves separate diagnostic evidence. `claude_cmd_for_stage` remains a legacy compatibility helper; do not execute its command string. See [provider runtime](../docs/provider-runtime.md) for exact precedence and permission behavior.

With `agents.<stage>.silence_timeout_seconds > 0`, `bureau-activity.py` observes useful events alongside the existing gate reader; the hard timeout remains independent. The child window requires a live owned tool process, not the provider leader or an unfinished item. Command matching seeds birth identities, peeling shell wrappers, environment assignments/`env` and `cd … &&` prefixes if exec already occurred; subsequent liveness follows those identities and observed descendants across exec. A background Bash result hands back a task ID without closing that association; `TaskOutput` can wait while the child retains its allowance, which ends when the identified processes exit. Detached ownership uses a per-call inherited marker plus sampled ancestry and birth identity; clearing the marker and detaching before sampling escapes identification. Stop and drain owned writers before returning `25` with `timeout_kind: silence`; `run_stage_for` consumes withdrawal before recording the human hold. Review passes a private cancellation file, polls all specialists under Bash 3.2 and joins siblings before its checkpoint and partial-round accounting. Only an actual silence failure (identified by the adapter's per-specialist `.silence` marker) or explicit cancellation stops siblings; ordinary Done, cancelled-ticket and inactive-state withdrawal lets every in-flight provider call finish, with the watchdog enabled or disabled. Only independent signals propagate `130`; acknowledged sibling shutdown preserves the originating stop. With the watchdog disabled, stream-reader failures remain observational gate-evidence errors and do not discard a successful validated final result. Do not convert silence into implementation's retryable wall-clock `124`, feed it into a verdict merger, or reuse a gate from an interrupted attempt. Outside commands keep their own timeouts.

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

Environment overrides (`BUREAU_SUPERVISOR_MAX_CRASHES`, `BUREAU_SUPERVISOR_STABILITY_WINDOW`) beat those JSON keys, then the defaults apply. After source selection, `bureau_number` accepts ASCII decimal digits only: crash limits 1–1000, stability windows 0–604800 seconds. Invalid selected values use 5/3600 rather than falling through to another source.

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

`start-bureau-v2.sh`'s `add_agent_window` now starts one `queue-loop-supervised.sh dispatch` pool. For dispatch mode, supervisor signals join owned nested runtime cleanup and return 130; the legacy stage-poller behavior above remains available by calling `queue-loop.sh` directly when no dispatcher owns the repository.

### Drain before refilling — single-flight + stage-priority

In active repos with many concurrent feature tickets, branches accumulate divergence faster than the pipeline can drain them. Cause: every cron tick, multiple branches re-run `merge_origin_main_or_abort` against an `origin/main` that advanced since last tick. Trivial conflicts auto-resolve, but real conflicts pile up and the loop spins. Two complementary knobs:

**A. Stage-priority sort in `queue-loop.sh`'s `all` mode.** Fan-out order is *reversed* from state-machine sequence — `merge`/`rebase` first, `spec` last. When multiple stages have pickable issues, attention goes to the ones closest to Done. Explicit `all` polling retains this order; launchers now use the continuous ticket pool below with downstream-first selection.

**B. `BUREAU_MAX_CONCURRENT_ISSUES` cap.** Repo-wide cap on how many *distinct* issues the bureau works on simultaneously. Default `0` = unlimited (current behaviour). Set to `1` with `agents.max_concurrent_include_merge: true` for single-flight through merge. With cap 1 and Merge excluded, the next spec can start while earlier tickets wait for a manual merge. Implementation:

- `count_in_flight_issues()` in `bureau-config.sh` queries Linear for issues in any state between Spec (inclusive) and Done (exclusive). Issues with parking labels (`needs-human`, `blocked`, `wip`) are excluded so a stalled issue doesn't deadlock the cap.
- `spec-pipeline.sh` checks one admission snapshot after the Linear precondition and before provider preflight; exits 28 (`spec-capacity-wait`) at cap, naming holding identifiers and states. The shepherd waits cancellably without stuck ticks or human labels; ticks keep 28 with outcome `waiting`. Merge is excluded by default under manual mode, included under auto, overridable with the boolean `agents.max_concurrent_include_merge`. Build Review always counts, including approved `--no-merge` stops there. Only spec gates — downstream stages keep running on already-in-flight issues, so cap=1 still drains the current ticket.
- Failed Linear snapshots are retried, then return 27; admission never treats an unreadable answer as an empty count.

Configuration in `.bureau.json`:

```json
{
  "agents": {
    "max_concurrent_issues": 1,
    "max_concurrent_include_merge": true
  }
}
```

Spec count/reservation/move is atomic within a common Git directory, including pending moves absent from lagging Linear counts. Separate repositories/hosts have no shared admission lock. Keep issue/workspace ownership and intentionally separated project scopes; single-flight configuration is not distributed coordination.

Before specify, fetch and prune all origin heads with the full refspec (`git fetch --prune --no-tags origin '+refs/heads/*:refs/remotes/origin/*'`), including in narrow clones. The allocator's per-issue reservation survives worker resets; check branch ownership and resume its local/origin branch before creating artifacts or calling specify. Fast-forward remote revisions without discarding unpublished local commits; diverged history stops with 17. `BUREAU_SPEC_LOCK_TIMEOUT` (process environment, whole seconds 1–86400, default 300) bounds allocator lock acquisition. Timeout prints the lock path, propagates 124 through the stage and returns the issue to Triage before specify. Git scanning inside the acquired lock has no new bound.

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
| 28 | spec-capacity-wait | full spec admission cap; quiet, cancellable wait |
| 29 | file-overlap-wait | reserved ticket/PR paths overlap; quiet admission wait, no paid pass or stuck increment |

`agents.file_overlap_gate` uses `bureau-paths.py` and the locked common-Git `bureau-admission.py` store before Build and before worker execution. PR 9's strict configured-ID checkpoint and shared run latch take precedence over both grants and overlap waits; callers consume the withdrawal, including Done's exit 0, before a Build mutation. Withdrawn reservations remain held through cooperative cleanup until execution ownership is gone; open PRs still hold their diff. Unknown scope is repository-wide; waiting Build tickets are elected from verified non-writing branches, never from state alone. Rework retains its granted reservation while pending task expansions stay in the inventory until admitted; other tickets compare against granted scopes only. Implementation/QA/Copy/review preflight rechecks holders before paid work. Evidence mirrors the wait without owning it. With the gate enabled, advisory `crosscheck-specs.sh` shares exact/directory path extraction and reports unknown scope as incomplete (4). Absent/false preserves the legacy extractor and substring matching; either mode remains an advisory warning.

Spec Review/UX call `bureau_overlap_ready` only after validation and pass `--status "$REVIEW_STATUS"` / `--status "$DESIGN_STATUS"`. Readiness records require the passing status and current canonical schema SHA-256 as well as the existing input identity. Legacy, mismatched or non-passing records are discarded and force a fresh provider pass; current passing records still resume admission without paid work on exit 29. Copy retains its existing readiness contract. Resync scripts/schemas together and restart drivers; no manual readiness migration is needed.
| 124 | timeout | bounded execution timed out |
| 130 | cancelled-run | invocation cancelled |

Other codes map to `error-N`; inspect the diagnostic rather than inferring completion. [Exit codes](../docs/exit-codes.md) and `bureau-config.sh:exit_class` define the current protocol.

`queue-loop.sh` captures the exit code, maps it to a class, and calls `alert_telegram` (throttled to max 1 alert per issue/pipeline/exit code per hour and repository via `<git common dir>/bureau/alert-throttle.log`; the alert names the repository). The alerter is a best-effort no-op when `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALERT_CHAT_ID` are unset, so dev environments don't break.

### Spec pipeline failure recovery

`spec-pipeline.sh` moves the issue from Triage → Spec **before** running speckit phases. To prevent stranding issues in Spec on failure:

1. **Preconditions before state mutation**: `precondition_linear` (exit 10) and `precondition_runner spec` (exit 16) run before the Spec move. Provider authentication uses the CLI authentication-status command; it does not spend a creative model call to test login.
2. **Recovery trap**: An EXIT trap (`_spec_recovery`) is installed immediately after the Triage→Spec move. On any non-zero exit during speckit phases (specify, plan, tasks, crosscheck, push), the trap routes the issue back to Triage with an explanatory comment and fires a Telegram alert.
3. **Trap cleared on success**: The trap is cleared (`trap - EXIT`) just before the final `move_issue → Spec Review`, so the success path doesn't trigger recovery.

Successful specify/plan/tasks phases each commit their reserved feature directory locally, then checkpoint the commit, input/review/research fingerprint and committed artifact hashes in the evidence v1 store (`checkpoint_version: 2`). Registered spec workers reset/clean normally to the last verified commit reachable on their reserved local or origin branch, discarding all uncommitted leftovers. Only a contiguous fingerprint/hash-matching prefix is skipped; changed inputs or failed review restart specify. Fresh retries reconcile the local feature branch with the fetched origin tip before specify/plan/tasks: fast-forward when behind, retain local-only descendants, or halt true divergence with exit 25 and a needs-human reason requiring a maintainer to reconcile both histories. A missing local feature branch starts at the remote tip; no force-push is used. Local phase commits survive the shared Git reset; no extra pushes are added. Checkpoints never authorize resetting unregistered/app/user checkouts, and interrupted ownership still needs explicit recovery. App adoption removes disposable registration and former-worker failure notes. Checkpoint I/O failures disable reuse for an ordinarily authorized fresh run; optional read/prune failures affect only the named ticket and cannot change a valid provider exit, including when an unrelated ticket is unreadable. Unavailable pruning or opt-out storage warns without aborting the provider. Review FAIL and missing-tasks routing survive failed invalidation. Changed reservations start fresh on the newly reserved branch. See [spec phase resume](../docs/stage-protocol.md#spec-phase-resume-and-governance). Specify and plan allow no measurements, probes, benchmarks or full test-gate runs. In spec.md, place `## Governance and security` with `Decision:` and `Rationale:` lines at column 0, in plain text, before any code block or HTML block; a leading YAML front matter block is allowed. The shared validator catches common placement, control-character and normalized-placeholder mistakes, including front matter, first fence/HTML cutoffs, code-span values and indented rationale continuations; see the exact rule and Known limits (governance check) in the linked stage protocol. It targets honest mistakes rather than deliberate obfuscation. Spec review judges whether the section is real and adequate, including circular/vague reasoning; its FAIL and detected validator errors both route back to Spec. App completion uses the same check and does not replace substantive review. Preserve customized Spec Kit templates.

This pattern ensures issues are never stranded in Spec with zero artifacts. The same recovery approach can be extended to other pipelines.

### Continuous ticket pool

Launchers start one `bureau-dispatch.py` pool through `queue-loop-supervised.sh dispatch`, replacing independent stage pollers. `agents.parallel_lanes` (default 2) is shared with batch orchestration; `BUREAU_MAX_CONCURRENT`/`--max-concurrent` are compatibility overrides. A common-directory lock excludes competing dispatch systems. Fresh per-attempt runtime identities and worktrees run shepherd with enforced `--respect-config` stage checks. Initial admission settles before another startup snapshot; existing overlap reservations remain authoritative. Only typed changes to complete snapshots retry, at most three times per admission in dispatcher mode, with the same gate and withdrawal checks. Spec count/reservation/move is serialized, including reservations absent from lagging Linear counts. Quiet waits yield with persisted backoff; 25 (including silence escalation) and unknown failures cannot become immediate repick loops. Shutdown joins only owned live runtime handles and retains interrupted work. Owned Merge recovery uses the existing conflict transaction, merge-forward, normal push, withdrawal checkpoints and fresh Build Review; report `refreshed`/`base_refresh` plus exit 20 is a handoff, never merge success. Standalone waits and strict merge gates remain. Dispatch requires explicit `agents.file_overlap_gate: true` even with one lane. A launch journal records each runtime outcome independently of its parent and is reconciled into skips before admission after restart; incomplete launches stay held. Supervisor lock contention waits cancellably without spending crash retries or signalling inherited lanes. Construction-time cancellation survives the run-handler handoff. See the [continuous dispatch protocol](../docs/stage-protocol.md#continuous-ticket-dispatch) for metadata, hold release and upgrade boundaries.

### Pre-spec research (optional, label-gated, best-effort)

For issues that integrate with external APIs/SDKs or pin to specific library versions, the spec agent's stale training data is a known failure mode — it confidently invents method signatures and config keys that don't exist. `spec-pipeline.sh` solves this with an **optional** research pass that runs immediately before `speckit-specify`, gated by:

1. The Linear label `needs-research` is present on the issue, **and**
2. `.agents.research` is configured in `.bureau.json` (either `true` or `{"model": "<id>"}` — typically a cheaper model like Haiku since the work is reading docs, not designing code).

When both conditions hold, `$CLAUDE` is invoked with WebFetch/WebSearch enabled and instructed to compile a markdown digest of the relevant APIs (current versions, endpoints/method signatures, recent breaking changes, gotchas). The output must start with the marker `<!-- bureau-research: <api-list> -->`. On success the digest is posted to Linear as a comment (visible to spec-review for traceability), injected into the `speckit-specify` prompt as `$RESEARCH_CONTEXT` (the spec agent is told to treat it as authoritative for API shapes), and the `needs-research` label is stripped.

The whole stage is **best-effort**: a failed research call (non-zero exit, missing marker, network down) is swallowed with `|| true` / `|| echo ""` and the pipeline falls through to specify without research context. The exit-code protocol (0/2/10–16) is untouched — no new class. The label is only stripped *after* a successful `post_comment`, so a crashed run leaves the label in place and the next pick retries naturally. Default is **off** (no `.agents.research` entry → `agent_enabled` returns false), so existing deployments are unchanged until opted in.

### Worktree ownership

Background entry points claim issue and checkout ownership through `bureau-runtime.py`, then use `bureau-worker.sh` for a registered disposable checkout. Reset refuses unregistered directories and requires a matching live claim; the existing non-spec exception requires an explicit former-worker failure note and re-registers a clean linked worktree whose HEAD is on the freshly fetched `origin/<branch>`, independently of base resolution. Implement can also retain an unregistered worker ahead only by verified clean base merges after a recorded ordinary failure 22/23 of this issue in this workspace. Resolve the merge-only base from a valid configured `repo.base_branch`, else the symbolic `origin/HEAD`, else `main`; an unresolved selected ref disables that rule. The shared predicate pins HEAD/issue/base, excludes base commits, and verifies two-parent ancestry plus exact clean merge trees; worker preparation and implement startup both bypass resetting checkouts. Ref/status snapshots are rechecked together; a changed HEAD or dirty tree refuses re-acceptance without resetting the retained work. A failing implement stage still pushes a merge-only state (one CI run), as before. A refused failure push leaves the merge available for the next tick. See [the exact acceptance and preservation rules](../docs/stage-protocol.md#implement-retries-with-clean-base-merges); interrupted or legacy/unknown failure records do not gain this reuse permission. Spec workers keep disposable registration after ordinary failures and reset/clean to committed phase checkpoints (see above); interrupted work still needs explicit recovery. The worker releases its own branch when finished. A branch held by another checkout still causes an ownership conflict, never an automatic detach. App work uses prepare/finish and preserves the current tree.

### JSON-block parsing for stage outputs

Every pipeline whose output the shell parses — `spec-review`, `ux`, `implement`, `qa`, `copy`, `code-review` — now instructs Claude to emit a trailing fenced `` ```json `` block with the structured fields the shell needs (verdict, status, ui_work_needed, strings_changed, etc.). The shell side uses `parse_claude_json` from `bureau-config.sh`, which `awk`-extracts the last `json` block and pipes it to `jq -r`. **Never** parse these outputs with `grep` / `sed` on the prose — the merger format drifts as the model varies (this drift is why commit `2397837` added the `VERDICT=${VERDICT:-BLOCK}` fallback in code-review). A regex parse that silently lands on "empty" and routes the happy-path without anyone noticing is worse than a loud parse failure.

Implementation, QA, Spec Review, UX and the review merger pass result schemas through the shared provider adapter. Spec Review and UX use `bureau-spec-review.schema.json` and `bureau-ux.schema.json`; missing, unknown, wrongly typed or schema-invalid results exit 22 with preserved edits and a human hold before ordinary push or routing. The review merger uses `bureau-review.schema.json`. A provider that enforces a schema returns only the object, so the merger's prose travels inside it: a required `comment` (markdown: specialist summaries, findings by class with file:line, fixes needed) and a required `findings` list of `{file, line, class, msg}`. The stage posts the comment and then the rest of the object as the fenced verdict, on the PR and in the Linear REQUEST_CHANGES and BLOCK comments, and reads the verdict from the object, never from the comment. Comments over 60,000 bytes are shortened in the middle (`bureau_cap_comment`): Linear and GitHub refuse longer ones.

When adding a new Claude call whose output needs programmatic consumption:
1. End the prompt with a fenced `` ```json ``` block showing the exact schema as a template (zeros / empty strings / empty arrays for defaults).
2. In the calling shell, use `parse_claude_json "$OUTPUT" '.field'`.
3. Validate the result through `--schema` and route only explicit known statuses. Spec Review and UX reject missing, unknown or wrongly typed statuses with exit 22 and `mark_needs_human`; a failed label write persists a local hold. Check ticket withdrawal before consuming the provider result or recording the hold.

### New agents: QA (mechanical) and Copy (opt-in)

- **`qa-pipeline.sh`** slots between Build and Build Review. It's a *mechanical* executor — runs `npm test` / `cargo test` / `pytest` etc. before any Claude call, only engages Claude on test failure or missing coverage. Its job is distinct from code-review: QA confirms the test oracle passes; code-review judges the code's correctness in paths tests don't cover. Running QA first also keeps code-review from burning tokens on a PR whose build is red. Opt-in via `states.qa` + `agents.qa: true`. When QA is configured, `implement-pipeline.sh` routes issues to QA first instead of directly to Build Review.

- **`copy-pipeline.sh`** slots between Design and Build (or wherever you route `needs-copy` issues). It's opt-in at both the state level (`states.copy`) and the label level (`labels.needs_copy.name`). It reads an optional voice guide (`repo.copy_voice_file`) and polishes user-facing strings — button labels, error messages, empty states. It leaves tests, logs, and code comments alone.

Both pipelines use the same JSON-block output convention and the same exit-code vocabulary as the other stages (10 / 11 / 12 / 16 for preconditions, 2 for queue-empty, 0 for success).

### New agents: Merge (gated) and Rebase (opt-in, force-pushes)

Reviewed PRs accumulate when nothing closes the loop: mergeable-and-approved PRs sit waiting for someone to click Merge, branches go DIRTY when main moves, and the 3-cycle loop-breaker escalates to needs-human without followup. The `merge` agent gates and merges; the `rebase` agent unsticks DIRTY bureau-only branches.

- **`merge-pipeline.sh`** picks issues from `BUREAU_STATE_MERGE` and merges only when EVERY gate passes:
  1. PR `state == OPEN`
  2. `mergeStateStatus == CLEAN` (GitHub heuristic — async-cached, lax when branch protection isn't strict)
  3. **`pr_ci_is_green` (bureau-enforced, NRSR)**: the newest check run per name and app ID on the PR's current head SHA must be completed without failure. Latest readable `started_at` wins, then highest `id`. Readable times use `YYYY-MM-DDTHH:MM:SS[.digits]` with `Z` or a numeric `±HH:MM` offset, preserving every fractional digit; a name/app group with any unreadable timestamp uses IDs alone. Superseded runs are excluded from pending, queue-grace and failure decisions. Only newest runs with conclusion `success` and legacy statuses in state `success` count toward `.agents.merge_min_required_checks` (default 1). Skipped and neutral runs never fail the head and never count; an all-skipped or all-neutral head passes with minimum `0`. Legacy statuses keep GitHub's latest result per context and their separate pending/failure handling. Independent of `mergeStateStatus` because CLEAN passes when no required-checks rule is configured. Toggle: `.agents.merge_require_green_ci` (default true; only the JSON `false` switches it off, any other value keeps it with a warning). A head with no check run and no status at all in the raw lists is `not yet` until its commit is older than `.agents.merge_ci_start_grace_seconds` (default 1800), blocked after. A newest check run still `queued` (taken by no runner) is `not yet` until its `started_at` is older than `.agents.merge_ci_queued_grace_seconds` (default 3600), blocked after with its own line (`ci: check <name> queued for N s on … — runner offline?`).
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

An unchanged saved requested stop retries the acceptance ledger's pending destinations before returning 20; it preserves the saved approval and runs no new review. Published evidence uses store-relative IDs and the literal `<git common dir>/bureau/evidence/v1` location, never absolute local URLs. Review receipts pin `review_inputs` (`head`, `base`, `validation_head`) so the renderer can verify an unpublished validation merge's exact parents. These observations confer no acceptance or merge authority. The sole advisory free-text exception, `insights.jsonl`, has a best-effort pre-persistence filter for credentials (including quoted names and recognizable split tokens), home paths and log/stack/prompt output; a rejection warns without changing stage success. Insights stay local and never appear in GitHub/Linear comments or the acceptance brief. `bureau_comment_projection` strips the field recursively from structured/enveloped/fenced results before comment formatting and size caps; comment parsing/projection failures never fall back to raw structured text or change status/verdict/routing.

The v1 evidence store in `<git common dir>/bureau/evidence/v1/` observes these boundaries without granting merge permission: provider receipts precede stdout, nested worker/stage attempts share one outer manifest, and review/QA/merge gate digests and allowlisted metadata survive worktree cleanup without log bytes or command text. Optional capture, cleanup and identity lookup exceptions preserve stage exits. Python warnings use unbuffered fd 2 writes to avoid final-flush exit 120; Bash evidence and review-cleanup diagnostics use protected subshells so SIGPIPE cannot terminate the stage. Only receipt persistence before forwarding provider success is mandatory. Every write requires confirmed capacity for its full atomic staging file and additional file slot. Pruning removes abandoned staging files; any pruning exception skips optional writes and prevents a mandatory success receipt. Gate results name the checked HEAD, not necessarily the final HEAD. `mark_needs_human`, approval/ownership stops and Merge routing open stable typed waits; repeated ticks preserve first reached time. Wait sweeps advance persisted cursors only after attempted reads, including timeouts. Selection reserves staging room for the next timestamp and checks an unchanged oldest cursor by writing and reading it back, using persisted order when that succeeds. Only refused persistence selects in-memory attempt order or a shell dispatcher's sweep count. Fresh shell ticks seed that fallback in configured poll-interval buckets. For a stable set of N readable open-wait tickets, an advancing clock and at least one attempted read per sweep, all are attempted within N sweeps with writable cursors; the fallback retains that bound in a continuous dispatcher or fresh ticks at the configured cadence, including 30-minute polls. Other restart/cadence patterns have no cross-process fallback bound. The merge scheduler's `stopped_at` still updates for backoff and is separate from evidence `reached_at`. `bureau-runtime.py waits` reads open waits and the union-based observed waiting share; unknown resolution times stay null. See [stage protocol](../docs/stage-protocol.md#versioned-background-evidence) for schemas and retention. App records and the exit/authorization protocol are unchanged.

**Important:** opting in to merge does NOT auto-approve anything. Code review still has to pass the 3-cycle loop-breaker first. The 3-cycle escalation to needs-human is a deliberate protection, not something to optimize around. Two human eyes on a first APPROVE remains cheap and is not a goal of this pipeline.

### Bounded retry loop in implement-pipeline.sh

`implement-pipeline.sh` runs the selected provider inside a `for (( i=1; i<=MAX_ITER; i++ ))` loop instead of a single call. Each iteration: invoke `$CLAUDE`, parse the strict JSON status block via `parse_claude_json`, run the squash-range check (a CI suppressor in a commit message, or a range that cannot be read, stops the loop as `CI_MARKER`), push whatever was committed (deferred to the end-of-run push while a PR is open when `agents.implement.push_each_iteration` is `false`), decide whether to continue. A pass whose provider call fails, or a timed-out one when no pass is left, ends the stage after pushing every commit origin lacks, in both push modes (`push_if_ahead`); every other way out before the end-of-run push (a signal, a crash, a failed `/goal` run) does the same from the EXIT trap. Confirmed ticket withdrawal retains this best-effort preservation push of commits already made, without another checkpoint; new PR create/ready, comments and merge remain guarded. The pipeline previously emitted that JSON contract but never read it back — every exit-0 run shipped to Build Review even on `status: PARTIAL`, leaking half-done work into review.

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

For Codex implement turns only, typed gate-only NEEDS_HUMAN holds or legacy `SANDBOX_GATE:` reasons enter the same machine-evidence reconciliation: matching result/command digests, one execution-bound cwd, current HEAD and a failed/aborted configured command are mandatory. One reserved outside rerun per gate/HEAD/stage attempt can clear exactly those holds. Other blockers and missing/stale proof stay blocked. Commit, hook, squash-range and merge gates remain in force; outside logs stay in normal stage logs and only metadata/digests enter evidence. See [Background providers](../docs/provider-runtime.md#codex-implementation-sandbox-gate).

| Status | PR | needs-human label | State move | escalations.log |
|---|---|---|---|---|
| `COMPLETE` | flipped to ready | no | → QA or Build Review | no |
| `NEEDS_HUMAN` / `STUCK` / `CAP_TIME` / `PARTIAL` | draft (visible to reviewers) | yes | none (stays in Build) | yes |
| `CI_MARKER` (squash-range check not clean; overrides every other status) | draft, report posted on the PR | yes | none (stays in Build) | yes |

PRs open as `--draft` during intermediate iterations so QA and code-review don't trigger on half-done work; flipped to ready via `gh pr ready` only on COMPLETE.

### Deterministic review convergence

Opt-in ADR grounding uses `build_review_adr_context <REVIEW_BASE> <REVIEW_HEAD>` after the final head pin. Only accepted records with identical paths and blob IDs in both trees are settled, including for Known-limit exclusions. Added, changed and removed records are non-authoritative proposals under review; their references are valid but never waive or downgrade findings. Keep untrusted context separate from included reference IDs. Validate optional `adr_refs` against only those IDs when enabled; malformed/unknown references block like other malformed finding fields. The pipeline selects `bureau-review-adr.schema.json` only when enabled. Disabled passes a null index and selects the byte-identical legacy `bureau-review.schema.json`, preserving provider validation, Codex wire schema, receipt schema digest and prompt/comment bytes; unsolicited `adr_refs` remain unexpected result fields at the provider boundary. Include the enabled context/settings digest in approval reuse policy. See [the review protocol](../docs/stage-protocol.md#review-convergence) for bounded local-object reads and metadata-only evidence.

Run `review_convergence` over the original merger result before `decide_review_verdict` applies security, build and the legacy cycle cap. `agents.review_min_severity` defaults to BUG; MINOR is stricter. Validate the fields and finding classes; never discharge BLOCK, CRITICAL, security/acceptance blockers or malformed/contradictory results. A nonempty list of deferred MINOR/SKIP findings can discharge REQUEST_CHANGES; an empty list cannot. Keep the provider object/digest unchanged and record the final shell decision separately. Render deferred/SKIP lists only from `bureau_comment_projection`, preserving local insight exclusion.

Keep `max_review_cycles` as the previous rework allowance. Apply the additional `max_review_rounds` total limit (default cycles + 1) after the existing cap: unresolved round N blocks with 25, while genuine approval may finish. `review_counts` prefers structured round/verdict markers and counts duplicate markers once; unmarked history falls back to `Code Review.*Changes Requested`. Do not rename that header, which the implementer also matches. Reserve a fresh round number in Linear before paid calls (a marker without a verdict); final comments add its verdict. A failed/interrupted attempt consumes the number without inventing a verdict or changing provider-error exits. Approval reuse and gate polling skip the reservation and add neither a round nor tokens.

`bureau_review_evidence` records the stage attempt's round and decision metadata. Sum the four phases' numeric receipt measurements by provider attempt ID, never by parsing prose or cost envelopes, and never charge a duplicate link twice. Missing phases/measurements yield null for the affected aggregate. Keep receipt persistence and PR 6's implement/QA-only sandbox recovery independent: outside green cannot clear a review BLOCK. These are background-stage rules; app prepare/finish does not gain a new autonomous review loop.

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


### Bounded advisory context: `build_lessons_context`

Spec, implement (both loops), QA and the review specialists call `build_lessons_context <stage> <spec-directory> [local-diff-range]`. `bureau-lessons.py` selects task/diff path matches plus untagged general sets, drops expired entries with one warning and caps the complete advisory block at 8192 UTF-8 bytes by default (`agents.lessons_budget_bytes.<stage>`). Budgets keep whole entries in deterministic order, skipping entries that do not fit and continuing to later entries, followed by the truncation marker; never slice an entry, even when one entry exceeds the whole budget. Review passes its pinned PR range; other stages use the configured local base. Preserve the wrapper, keep advice outside gate decisions and never write back to `LESSONS.md`. Legacy free-form files remain one bounded general set; resync the helper and callers together. The format and deterministic ordering are specified in `docs/stage-protocol.md`.

### Interrupted worker recovery

A cancelled background run keeps its issue/workspace leases and revokes disposable-worker registration. An exited shell does not prove that nested stage processes stopped. Inspect saved work and live processes before `bureau-runtime.py release RUN`; release refuses known live writers or unavailable process inspection. The checkout stays unregistered after release, ready for explicit app adoption or retirement: the interrupt message names the release command, the worktree and its unpushed local branch, and a rerun that meets the claim or the unregistered worktree stops with 21, sets `needs-human` and comments the same steps once. Never reset it automatically to recover from cancellation.

### Ticket state as a cooperative kill switch

Use `bureau_ticket_checkpoint ISSUE` at safe boundaries; only the additional refresh may warn once and continue on a read failure. Use its `strict` mode for initial dispatch, or pass an already-read strict snapshot from the optimistic mutation guard. IDs come from configured state keys in `agents.ticket_active_states` (default: configured triage through Merge states). Done is 0; configured cancelled/canceled/duplicate is 26; every other inactive ID is 25. Unknown display names confer no authority. Active-to-active mutation conflicts remain 21; signal cancellation remains 130.

The runtime shares `bureau/ticket-control/RUN_ID` outside the worktree. Stop markers latch across subprocess captures and parallel passes; the `latched` mode in parents consumes an existing stop without another refresh. It is not a replacement for the before/after provider refreshes. Join all launched reviewers before consuming the latch: errexit on the first withdrawn specialist must never free a checkout while another specialist still writes. The provider wrapper checks both sides of every call, including best-effort research/digest. Publication helpers and actual push/PR/merge call sites check again. Outside-sandbox gate recovery checks before execution and after return. Acceptance delivery checks both destinations through the same shell helper; a Done exit 0 without its authorization sentinel is a stop, never successful delivery.

Recovery traps must honor the latch. Spec checks before any failure rollback, and never rolls back a signal or a confirmed withdrawal. Implement's failure/EXIT preservation push deliberately skips checkpoints and retains the stage's original exit code; it only sends commits already made. Disposable cleanup treats a withdrawn Done=0 as unfinished when dirty/ahead, exempts withdrawn Spec from its ordinary retry reset, removes worker registration and preserves files/commits/branch. Runtime lease release then follows ordinary completion, after all writers have joined. This is cooperative stopping: latency includes the current provider call, and a failed extra refresh does not revoke an already authorized unit. Additional reads are scoped to a claimed background run and cannot alter the caller's Linear retry mode or fault record; standalone helpers retain their existing reads. Existing mandatory state, ownership and mutation checks stay strict.

### Local failure learning

`bureau_insight_cause` in `bureau-insights.sh` consumes only trusted exits, provider outcome/timeout metadata and explicit shell signals; it never parses cause claims from model prose. `collect_stage_outcome` captures every finalized background stage, including worker refusals with no nested stage and failures with no provider result. Keep withdrawal 0/25/26, silence 25, quiet overlap 29, review rounds/floor and dispatcher outcomes independent of this optional store. `bureau_outcome_signal` marks trusted review caps or shell gate/hold decisions; it cannot alter a verdict. `collect_insights` now accepts all stages/exits. `collect_dispositions` projects only status/severity/filtered repository-relative path plus provenance, using SKIP and the configured severity floor for skip/deferred and treating implement applied/declined as advisory self-reports. No finding prose or reasons enter evidence. All learning ledgers share the existing evidence lock, privacy filter, staging budget and 2,048-row/30-day per-ledger collection retention. Strip `review_dispositions` recursively alongside `insights` from every comment/brief projection. Shepherd outcome events use the queue's existing fields and run before the post-worker checkpoint; neither event nor ledger failures authorize further work. See the [failure learning protocol](../docs/stage-protocol.md#failure-learning-records).


### Structured review merge and source checks

Keep specialists parallel and consume full structured results with `bureau-review.py` before the legacy merger's tail cap. Use the model merger only when a specialist result is missing/malformed; preserve readable critical/security/source floors and record fallback as metadata. The comment headers, Changes Requested handoff and terminal round markers are protocol consumers, not presentation choices. Shell convergence and `decide_review_verdict` still decide routing. Pinned anchors read Git blobs from base/head, including base-side deleted code; `review_grounding: required` adds BLOCK/25 for invalid references while parse errors stay 22. The opt-in finding verifier is bounded shadow evidence only. See the [review protocol](../docs/stage-protocol.md#review-convergence) for schemas, scopes and budgets.
