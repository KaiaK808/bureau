# Troubleshooting

When the pipeline misbehaves, start here. Start with `python3 scripts/bureau-doctor.py` (or the source installer’s `doctor` action before scripts are installed). It reports config provenance, interfaces, effective providers and drift without making remote calls.

---

## Setup

### `MISSING: <tool>` at Phase 0

Install the tool reported for the selected mode, then rerun setup. App tasks need the portable helpers but do not require a background Claude/Codex CLI or tmux. Background mode needs the selected provider executables; GitHub operations need `gh`, and tmux is required only for tmux launchers. Use `python3 scripts/bureau-doctor.py --mode app` or `--mode background` to check the appropriate dependencies.

### `This repo already has a bureau pipeline configured`
- `/bureau-init --update` — change teams / labels / states / agents
- Update the installed Bureau source first, then request scoped interface/script resync in each target repository; see [migration](migration.md)
- Preview the [additive migration](migration.md); preserve existing IDs and settings

### Linear MCP not available during setup
Discover the available Linear connector in the current host. In Claude Code, use `/mcp`; in Codex, use the installed Linear tool capabilities. Shared runtime transitions still require the configured API key.

### `LINEAR_API_KEY` not set
Interactive setup completes without it, but agents fail on first run. Add to `.env` before launching:

```sh
echo 'LINEAR_API_KEY=lin_api_xxxxxxxxxxxx' >> .env
```

---

## Pipeline runtime

### Agents running but not picking up issues
Check the issue:
- Has the eligibility label (e.g. `lane-2`) — `pick_issue` filters by label *name*
- Is in the `Triage` state for the team configured in `.bureau.json`
- Has no parking labels (`needs-human`, `blocked`, `wip`)
- Is not blocked by another issue (Linear `blockedBy` relation)

### Pipeline exits 2 every tick despite eligible issues
Check whether `agents.max_concurrent_issues` is set. If yes, an in-flight issue (or several) is occupying the cap. See [recipes → single-flight](recipes.md#single-flight-pipeline). Find them with:

```sh
grep "in-flight cap reached" logs/queue-spec.log
```

### `⚠ bureau-init template drift: N differ, M new`
The skill template has been updated since this repo was initialized. Run:

```sh
# In the current assistant: $bureau-init --resync-scripts (Codex)
# or /bureau-init --resync-scripts (Claude)
```

Inspect the deterministic preview. Unknown/customized files stop the selected asset batch; resolve each named conflict while preserving local changes, then reapply the coherent batch. `--update` changes configuration only. See [migration](migration.md) for source update and per-target resync steps.

### Issue stranded between states
The spec pipeline installs an EXIT trap that routes back to Triage on crash. If an issue is genuinely stuck:

1. Check `logs/escalations.log` and the latest issue comment — background escalation sites log after a successful human-label update. `grep ESCALATED logs/escalations.log | grep EXP-XXX`.
2. Check `logs/queue-<stage>.log` for the last error.
3. Check `logs/supervisor-<stage>.log` if you're using `queue-loop-supervised.sh`.
4. Inspect `bureau-runtime.py status` and the saved run before reconciling state; stale results cannot safely advance it.

If the issue was in Build and crashed, the worktree under `.worktrees/queue-implement/` may have uncommitted work. Inspect before resetting.

### Pipeline keeps hitting `merge_origin_main_or_abort` conflicts
Symptom: every cron tick re-runs the merge, conflicts pile up, the same branch reappears tick after tick.

Two complementary fixes:

1. **Single-flight mode** (`agents.max_concurrent_issues: 1`) — branches don't race against each other.
2. **Trivial-conflict resolver** — already shipped; lockfiles, generated files, and known-mergeable patterns auto-resolve.

Non-trivial conflicts stop the stage; inspect its exact exit class and preserved checkout. `upstream-port.sh` uses exit 17 for patch conflicts. Do not repeatedly reset or rerun a conflicted worker.

---

## Exit-code symptom map

Each pipeline script exits with a classified code — see `docs/exit-codes.md` for the canonical table. What to do when you see each:

### Exit 10 (linear-down) — Linear API unreachable or auth-rejected

The Linear precondition reports exit 10 when the API check fails. Inspect its response and the trusted API-key configuration before changing ticket state; do not diagnose it by starting a model.

- Check `.env` — `LINEAR_API_KEY` set and not expired?
- Check Linear's status page. Rare, but happens.
- `curl -sS -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" -d '{"query":"query { viewer { id } }"}' https://api.linear.app/graphql` — should return your user id

### Exit 11 (worktree-dirty) — uncommitted changes block progression

The stage requires a clean checkout but finds local changes, possibly preserved from an interruption.

```sh
python3 scripts/bureau-runtime.py status
git worktree list
# Inspect the exact workspace reported by the run:
git -C "$BUREAU_WORKSPACE" status --short
```

Inspect the owner and save the work before deciding how to resume. Existing unregistered workers and app checkouts cannot be made disposable by clearing files. Do not reset, clean, forcibly remove or detach another task's checkout. See [ownership and resume](stage-protocol.md).

### Exit 12 (no-branch) — `bureau-branch:` marker missing

Downstream stages resolve the branch via a `<!-- bureau-branch: ... -->` marker comment posted by spec-pipeline. Exit 12 means the marker is absent or points at a non-existent branch.

- Check the Linear issue's comment history — is the digest comment there?
- Reconstruct a missing marker only from the verified issue branch and completed spec evidence. Reconcile the issue state and use the selected app stage or named-ticket driver; `queue-loop.sh spec 1` polls all eligible tickets at a one-minute interval and is not an issue-specific repair.

### Exit 13 (no-tasks) — `tasks.md` expected but missing or unmatched

Confirm the approved feature directory contains `tasks.md` and matches the issue's canonical branch. Do not infer a `codex/*` branch from Linear's generated branch name. For retained Spec Kit helpers, pass both `SPECIFY_FEATURE` and `SPECIFY_FEATURE_DIRECTORY` for that exact feature; see [legacy migration](migration.md). Inspect ambiguous numeric prefixes instead of guessing another feature.

### Exit 14 (build-failed) — implement / upstream-port build gate failed

The build command (`cargo build`, `npm run build`, `bash tests/run.sh`, etc.) returned non-zero. For **upstream-port.sh**, the command comes from `repo.upstream_port.build_cmd` — verify it's right for the target repo.

- Tail: `tail -40 /tmp/upstream-port-build.*` (or check the pipeline log)
- Common culprit: your build depends on a service (postgres, redis) that isn't running in the worktree

### Exit 15 (no-pr / test-failed) — test gate failed OR expected PR doesn't exist

**Overloaded code.** In queue-loop.sh scripts: no PR was found for the branch after the stage that should have created one. In upstream-port.sh: the test command (`repo.upstream_port.test_cmd`) returned non-zero.

- For "no-PR": check `gh pr list --head <branch>` — was one created? Was it closed manually?
- For "test-failed": look at the tail of `/tmp/upstream-port-test.*` for the failing test names

### Exit 17 (rebase-needed) — `git apply --3way` produced conflicts

In **upstream-port.sh**, configured path translations have been applied but `git apply --3way` still has content conflicts. Inspect the specific caller when this class appears elsewhere.

- If appropriate, retry with `--with-llm` for the configured `upstream_port` provider; the explicit cost gate applies and non-interactive use also requires `--yes`
- Or escalate to a full shepherd ticket: create a Linear issue with the upstream link and let the normal pipeline handle it

### Exit 18 (gh-failed) — overloaded catch-all for `gh` failures

`upstream-port.sh` uses exit 18 for GitHub failures and preflight errors such as invalid arguments, dirty checkout or an existing branch. Other scripts can propagate a different status; read the emitting command.

- First: `gh auth status` — token expired?
- Rate limit: inspect `gh api rate_limit` and the reported reset time
- Branch protection: check if the PR needs a review approval you don't have (bot accounts sometimes hit this)

### Exit 19 (rebase-rejected) — `git push --force-with-lease` refused

`rebase-pipeline.sh` uses `--force-with-lease` for safety. Refusal means someone else pushed to the same branch since your `git fetch`.

- Preserve the local rebased HEAD, fetch and compare the newly published commits before any retry. Refreshing the remote-tracking ref and immediately force-pushing can defeat the protection that just stopped the run.
- Reconcile the other writer's work and ownership before a separately authorized push.

### Exit 1 / 128 / 141 — `error-<N>` catch-all

Anything outside the classified table maps to `error-<N>` in `queue-loop`'s alert throttling. Usually a bug in the pipeline script or an unhandled bash error.

- `128` from git: inspect `git worktree list` and runtime ownership. A held branch requires a coordinated handoff, not forced removal of the other checkout.
- `141` indicates SIGPIPE. Inspect whether a consumer exited early and whether the stage produced a valid result; do not count this as a successful stage automatically.
- `1` catch-all: read the last 20 lines of stderr; that's where the actual error will be.

---

## Driver failures (shepherd / orchestrate / upstream-port)

### `shepherd.sh` bailed mid-run — how to resume

Inspect the issue's current state, blocker comment, saved run and checkout. Resolve the cause and any human label before rerunning `bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123`. The state guard skips completed stages, but an unfinished/unregistered checkout can still refuse reuse; preserve its work and reconcile ownership first. Use a dry-run to inspect routing only. Exit 20 is the requested review stop, 25 is pause/human attention, and 26 is a cancelled ticket; none is Done.

### `orchestrate.sh --execute` — one lane failed, do the others continue?

**Yes** — independent lanes can continue. A serial chain stops at its first nonzero result, including review-stop 20 and human-attention 25. Dependent tickets are not complete merely because their predecessor reached review.

To recover:

```sh
# See lane status
./scripts/bureau-status.sh

# Rerun a specific lane
./scripts/shepherd.sh --no-tmux --no-merge TEAM-9 --worktree .worktrees/shepherd-lane-2
```

Inspect that lane's saved work and ownership before reuse. After resolving the blocker, recheck the schedule and current ticket states; completed tickets are skipped, but preserved workers are not automatically reset.

### `upstream-port.sh` exit 15 — test gate false-negative

Sometimes the test command passes locally but not inside the worktree — usually a service dependency (postgres, redis) or a missing env var.

- Tail the failing test names from the log
- If it's environment: set the needed env in `.env` and re-run
- If flaky, reproduce and record the failure. Correct the test or document any explicitly accepted exclusion; silently skipping it does not establish a passing acceptance gate.

---

## Stage-specific failures

### Merge gate refuses to merge a PR that looks green

`merge-pipeline.sh` enforces THREE independent gates: `pr_ci_is_green` (all check-runs completed + success), `pr_base_is_current` (`baseRefOid == origin/main HEAD`), and `mergeStateStatus == CLEAN` (GitHub's own answer). All three must pass.

To diagnose:

```sh
gh pr view <N> --json statusCheckRollup,baseRefOid,mergeStateStatus
git fetch origin && git rev-parse origin/main
```

- **`mergeStateStatus` isn't CLEAN**: usually `BEHIND` (need to rebase) or `BLOCKED` (missing review approval)
- **`baseRefOid` doesn't match `origin/main`**: main has moved since the PR opened. Rebase.
- **`statusCheckRollup` has a `FAILURE` or an in-progress check**: wait or fix the failing check

GitHub's mergeability state is cached. Bureau independently checks actual head checks and current base state, and repeats the gates just before merging. Background review of a stacked PR uses its actual PR base; that does not weaken the separate merge gate.

### Code-review hit `BUREAU_MAX_REVIEW_CYCLES` — what now?

`agents.max_review_cycles` (default 3) caps how many `REQUEST_CHANGES → Build → Build Review` round-trips before parking.

- Read the last review comment on the PR — it lists what the reviewer keeps flagging
- Resolve substantive findings or explicitly adjudicate disagreements; preserve the required CI/base gates
- If changing the reviewer, use the compatibility-aware Models update flow and confirm the effective provider/model

The background pipeline counts matching Changes Requested comments in Linear. Restarting it does not remove that history.

### QA returned `NEEDS_HUMAN` verdict

QA parks a ticket with `needs-human` when it can't decide whether a failure is legitimate (test genuinely fails) or spurious (env issue, flaky test, missing service).

```sh
# See what QA saw
grep "$ISSUE" logs/events.jsonl | jq -r 'select(.event=="qa_verdict")'
```

Typically: pull the branch locally, run the tests yourself, and either fix the code or mark the test as skip/ignore with a rationale.

### Codex-stage-runner failed spuriously

Current pipelines use `bureau-provider.py`; the old `codex-stage-runner.sh` command-string wrapper is a compatibility entry point. Inspect `logs/provider-runs/RUN/`, the resolved provider settings and exit class. Codex implementation/QA are supported through the shell executor, which handles Git publication and independent `repo.test_command` execution. A sandbox denial or missing service is an environment blocker (24), not proof that every write/test stage must use Claude. Resync the coherent runtime if an old unsafe-stage warning remains, then qualify the required capabilities; do not disable sandboxing or silently switch providers.

## Operational

### Agents pause and don't restart — session throttle triggered

Inspect the selected provider's usage signal and `session.usage_threshold_pct` (default 80). Claude can use `BUREAU_USAGE_FILE` or its legacy sources; Codex uses `BUREAU_CODEX_USAGE_FILE` or a shared file tagged `"provider":"codex"`. Numeric `updated_epoch` and `reset_epoch` fields use Unix seconds.

Missing signals allow dispatch. With `pause_on_stale_data: false`, signals older than five minutes are ignored; true continues applying the threshold to their reported percentage. A bounded tick returns until a later invocation instead of sleeping. `BUREAU_DISABLE_THROTTLE=1` deliberately bypasses only this guard. Also inspect the separate `bureau-runtime.py pause` marker before assuming quota is the cause.

### Worktree collision — "branch already checked out at another worktree"

Git refuses to attach a branch already held elsewhere. `free_branch_from_other_worktrees` reports the holder and does not detach it. Inspect `git worktree list` and `python3 scripts/bureau-runtime.py status`, contact the owner and arrange a handoff. An old-looking path is not proof of abandoned work. Only remove a checkout after its owner, saved work and interrupted processes have been reconciled.

### Rebase-pipeline aborted mid-rebase

The pipeline attempts `git rebase origin/main` only for Bureau-owned divergence. On conflict it calls `git rebase --abort` and requests human attention, so a completed failure normally leaves no rebase in progress. Inspect the exact worker with `git status` first. Preserve its HEAD and changes, then perform the appropriate manual reconciliation; use `git rebase --continue` only if Git actually reports an active rebase. Resolve the blocker before removing the human label and resuming.

### Using `BUREAU_DRY_RUN` to debug a specific stage

Use a queue preview for one stage or a named-ticket shepherd preview:

```sh
BUREAU_DRY_RUN=1 bash scripts/queue-loop.sh code-review 1
BUREAU_DRY_RUN=1 bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123
```

The queue example continues polling until stopped; `1` is its interval in minutes. Previews can read Linear, but do not invoke creative work or reset workers. Calling a background pipeline directly inside an app checkout can correctly return ownership conflict 21 instead of performing a preview. Use the app prepare/finish protocol for current-task work.

## Token-efficiency layers

### Implement parks `status=STUCK` on a ticket that's actually complete

Inspect the current result, task marks and branch commits before changing the loop. The portable loop treats an unproductive first `PARTIAL` iteration differently from a valid `COMPLETE` result; it also rejects an empty completion without branch commits. Older installations had different stuck-detection rules. Update source and resync scripts coherently before diagnosing an obsolete path. Claude `/goal` is optional and does not replace Git/test evidence; Codex retains the bounded loop.

### `headroom: command not found` after `headroom_wrap: true`
The flag is read live; turning it on doesn't auto-install Headroom. Install on the host running the pipeline (typically the operator's local box, not the CI runner):

```sh
pip install "headroom-ai[all]"
headroom --version    # confirm
```

If `pip` resolves to a Python that doesn't have user-level installs on PATH, prefer `pip install --user` and add `~/.local/bin` to PATH, or use a venv. The provider adapter checks for the wrapper and reports a missing executable.

### Caveman style leaked into a commit message or PR body
By design, caveman is scoped to per-stage review prose only — commit messages and PR titles/bodies stay in normal register. If you see telegraphic caveman-speak in commits or PRs, the leak path is via the per-stage prompt prefixes in `code-review-pipeline.sh` getting applied where they shouldn't.

Audit: `grep -n "caveman\|/caveman" templates/scripts/code-review-pipeline.sh templates/scripts/merge-pipeline.sh`. The prefix should only appear in review-prose construction sites. If it's in a commit-message or PR-body builder, that's the bug — file an upstream ticket and pin `caveman_level: "off"` until fixed.

### `/goal` is a no-op (turn loops forever or exits without evaluation)

Verify that the selected Claude installation supports `/goal`. The opt-in path does not automatically detect every unsupported CLI behavior. Disable `agents.use_goal_loop` to use the portable bounded loop, or qualify a compatible Claude installation before enabling it. The adapter applies the total timeout; Codex ignores this Claude-only flag.

## tmux

### `WARNING: Old 'bureau' session is still running`
The launcher found a legacy session named `bureau`. Inspect it and its workers before starting another dispatcher; do not delete the warning or kill a session whose owner/work is unknown.

### tmux session already exists on startup

The launcher recreates its own named session. Before rerunning it, pause dispatch and inspect active workers; killing or recreating a tmux session alone does not prove nested provider processes stopped. Preserve unfinished work and runtime leases until inspection confirms a safe handoff. Use `tmux ls` to identify the exact session rather than terminating unrelated sessions.

### Pane shrunk to 0 rows / "no space for new pane"
Already fixed — `start-bureau-v2.sh` re-tiles after every split. If you see this on an old install, run `/bureau-init --resync-scripts`.

---

## Linear state divergence

### PR is OPEN but Linear says Done
The verify-merge fix in `code-review-pipeline.sh` should prevent this — after `gh pr merge`, the pipeline confirms the actual PR state before claiming Done. If you see this on an old install, resync scripts.

### Issue marked Done but no PR exists
Check the bureau-branch comment marker. If it points at a branch that was deleted before merge, the pipeline may have lost the PR reference. Manually re-open the issue and route through Build Review.

---

## Claude / auth

### Pipeline fails with `claude-unauth` (exit 16)

Exit 16 now covers the selected provider. Inspect `claude auth status --json` or `codex login status` and complete that provider's normal login flow. Bureau checks authentication without a probe generation. This is separate from the Linear API key used for ticket CRUD.

### Headless Claude calls hang or timeout

Inspect the adapter's preserved stdout/stderr and result metadata before retrying. Confirm provider login, network availability and the configured timeout. Exit 124 indicates the bound was reached, 130 cancellation, and 24 an environment/permission failure. A retry requires inspection of interrupted ownership; avoid an unbounded probe or a permissions bypass as a diagnostic shortcut.

### Speckit phases produce empty `tasks.md`
The spec pipeline routes back to Triage automatically. To debug:

```sh
ls -la specs/<NNN-feature>/
cat logs/queue-spec.log | tail -100
```

Most common cause: the Linear issue description was too thin for `/speckit-tasks` to extract anything. Add acceptance criteria to the description and re-trigger.

---

## Supervisor

### Telegram alert: "supervisor giving up"
The supervisor crashed `BUREAU_SUPERVISOR_MAX_CRASHES` times in a row. The alert includes the tail of `logs/queue-<mode>.log`. Common causes:

- Bash syntax error introduced by an unfinished script edit
- Missing config key after a partial `/bureau-init --update`
- `LINEAR_API_KEY` revoked

After fixing, restart with `./scripts/start-bureau-v2.sh` or just re-run `./scripts/queue-loop-supervised.sh <mode> <interval>`.

### Crash counter stuck high
Restart the supervisor — counters are in-process, so a fresh launch resets them. Or wait `BUREAU_SUPERVISOR_STABILITY_WINDOW` seconds (default 1 h) of clean runtime for the auto-reset.

---

## Drift / upgrades

### Old commands at `.claude/commands/speckit.*.md`

Older Spec Kit integrations used command files. Refresh Spec Kit separately with the pinned installer and intended targets, preserving the active integration unless a switch is requested. Verify the new `.claude/skills` and/or `.agents/skills` entry points before reviewing obsolete files for removal. Preserve customized command content; do not delete the old glob blindly.

### Constitution missing after speckit resync

The current installer snapshots and restores an existing constitution, including on a failed Spec Kit invocation; it does not promise a persistent `.bak` file. Stop further resync, inspect the installer error and your pre-upgrade backup, and restore the actual constitution rather than generating a replacement. See [migration and rollback](migration.md).

## See also

- [Configuration](configuration.md) — config keys + env vars
- [Exit codes](exit-codes.md) — what each failure code means
- [Recipes](recipes.md) — common config patterns

## App ownership and permissions

An ownership conflict is not permission to detach another checkout. Inspect `bureau-runtime.py status`, locate the owner, and resume or perform an authorized handoff. `release RUN_ID` refuses a live background owner. App leases do not expire automatically: release only after confirming the old task has stopped. Unfinished workers retain progress and lose their disposable registration so later ticks cannot erase it.

For exit 24, inspect the exact denied operation. Codex leaves Git commits to the shell executor, and the executor independently runs configured implementation/QA tests. App test actions run under app permissions. Grant a needed capability through the supported host flow or report the environment blocker; do not weaken merge gates or report unrun tests as passed.

A prepared stage records the current branch, canonical issue marker, state, config and HEAD. If any change unexpectedly, reconcile the actual branch/ticket and prepare a fresh run. Never rewrite a result's HEAD merely to pass validation.

## Quota and missing cost

Codex does not consume ClaudeWatch signals. Configure a provider-tagged usage signal, or use the app's account-usage tool for interactive supervision. `null`/`unavailable` cost means the CLI supplied no estimate; it does not mean the work was free. A bounded tick waits until a later invocation instead of sleeping through the schedule.
