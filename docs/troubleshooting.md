# Troubleshooting

When the pipeline misbehaves, start here. Most symptoms have a one-line fix.

---

## Setup

### `MISSING: <tool>` at Phase 0
Install the named tool, then re-run `/bureau-init`. macOS one-liner:

```sh
brew install jq tmux gh
```

### `This repo already has a bureau pipeline configured`
- `/bureau-init --update` — change teams / labels / states / agents
- `/bureau-init --resync-scripts` — pull the latest pipeline scripts
- `rm .bureau.json` — start from scratch

### Linear MCP not available during setup
In Claude Code, run `/mcp`, authenticate `linear-server`, restart the session.

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
claude /bureau-init --resync-scripts
```

Per-file confirmation, ~40-line diff preview. Local tweaks preserved unless you confirm an overwrite.

### Issue stranded between states
The spec pipeline installs an EXIT trap that routes back to Triage on crash. If an issue is genuinely stuck:

1. Check `logs/escalations.log` first — every `needs-human` event lands here as one TSV line. `grep ESCALATED logs/escalations.log | grep EXP-XXX`.
2. Check `logs/queue-<stage>.log` for the last error.
3. Check `logs/supervisor-<stage>.log` if you're using `queue-loop-supervised.sh`.
4. Manually move the issue back in Linear.

If the issue was in Build and crashed, the worktree under `.worktrees/queue-implement/` may have uncommitted work. Inspect before resetting.

### Pipeline keeps hitting `merge_origin_main_or_abort` conflicts
Symptom: every cron tick re-runs the merge, conflicts pile up, the same branch reappears tick after tick.

Two complementary fixes:

1. **Single-flight mode** (`agents.max_concurrent_issues: 1`) — branches don't race against each other.
2. **Trivial-conflict resolver** — already shipped; lockfiles, generated files, and known-mergeable patterns auto-resolve.

For non-trivial conflicts, the pipeline exits 17 (rebase-needed) and routes for human intervention.

---

## Exit-code symptom map

Each pipeline script exits with a classified code — see `docs/exit-codes.md` for the canonical table. What to do when you see each:

### Exit 10 (linear-down) — Linear API unreachable or auth-rejected

Every stage hits the Linear GraphQL endpoint before any state mutation. Exit 10 means either the API returned an error or `LINEAR_API_KEY` isn't valid.

- Check `.env` — `LINEAR_API_KEY` set and not expired?
- Check Linear's status page. Rare, but happens.
- `curl -sS -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" -d '{"query":"query { viewer { id } }"}' https://api.linear.app/graphql` — should return your user id

### Exit 11 (worktree-dirty) — uncommitted changes block progression

`precondition_clean_worktree` refuses to proceed with staged or unstaged changes in the target worktree. Typically someone made a manual edit or a prior tick was killed mid-work.

```sh
cd .worktrees/queue-<mode>            # or shepherd, or shepherd-lane-N
git status
git stash                             # or git reset --hard, if you're sure
```

Then re-run. Don't `rm -rf` the worktree — use `git worktree remove`.

### Exit 12 (no-branch) — `bureau-branch:` marker missing

Downstream stages resolve the branch via a `<!-- bureau-branch: ... -->` marker comment posted by spec-pipeline. Exit 12 means the marker is absent or points at a non-existent branch.

- Check the Linear issue's comment history — is the digest comment there?
- If deleted or edited, re-run spec-pipeline: `./scripts/queue-loop.sh spec 1` for the issue

### Exit 13 (no-tasks) — `tasks.md` expected but missing or unmatched

Implement-pipeline resolves the tasks.md file by matching the branch's leading number (e.g. `091-foo`) to a spec dir (`specs/091-*/tasks.md`).

- Confirm `specs/NNN-slug/tasks.md` exists for the branch you're on
- If the branch name got truncated by Linear's auto-branch feature, the number should still match — the fallback slug-substring match only kicks in for legacy branches without numeric prefix

### Exit 14 (build-failed) — implement / upstream-port build gate failed

The build command (`cargo build`, `npm run build`, `bash tests/run.sh`, etc.) returned non-zero. For **upstream-port.sh**, the command comes from `repo.upstream_port.build_cmd` — verify it's right for the target repo.

- Tail: `tail -40 /tmp/upstream-port-build.*` (or check the pipeline log)
- Common culprit: your build depends on a service (postgres, redis) that isn't running in the worktree

### Exit 15 (no-pr / test-failed) — test gate failed OR expected PR doesn't exist

**Overloaded code.** In queue-loop.sh scripts: no PR was found for the branch after the stage that should have created one. In upstream-port.sh: the test command (`repo.upstream_port.test_cmd`) returned non-zero.

- For "no-PR": check `gh pr list --head <branch>` — was one created? Was it closed manually?
- For "test-failed": look at the tail of `/tmp/upstream-port-test.*` for the failing test names

### Exit 17 (rebase-needed) — `git apply --3way` produced conflicts

Only fires in **upstream-port.sh**. Config-driven path translations (`.bureau-port-map.json`) already tried; the diff still has content conflicts.

- Re-run with `--with-llm` to let Claude take a swing at translation (opt-in, cost gate applies)
- Or escalate to a full shepherd ticket: create a Linear issue with the upstream link and let the normal pipeline handle it

### Exit 18 (gh-failed) — overloaded catch-all for `gh` failures

Broader than the exit-codes table suggests. Any `gh` sub-command failure (auth, rate-limit, permission, branch-protection rejection) surfaces as 18.

- First: `gh auth status` — token expired?
- Rate limit: `gh api rate_limit` — under 100 requests-remaining, cool off for an hour
- Branch protection: check if the PR needs a review approval you don't have (bot accounts sometimes hit this)

### Exit 19 (rebase-rejected) — `git push --force-with-lease` refused

`rebase-pipeline.sh` uses `--force-with-lease` for safety. Refusal means someone else pushed to the same branch since your `git fetch`.

- `git fetch origin && git push --force-with-lease` — should now succeed if the concurrent pusher is you (different terminal)
- Otherwise: someone/something else is pushing. Investigate before retrying.

### Exit 1 / 128 / 141 — `error-<N>` catch-all

Anything outside the classified table maps to `error-<N>` in `queue-loop`'s alert throttling. Usually a bug in the pipeline script or an unhandled bash error.

- `128` from git: often a worktree collision (`git worktree add` where the branch is already checked out elsewhere). Run `git worktree list` — remove the stale worktree with `git worktree remove --force`.
- `141` from a piped command: SIGPIPE, usually harmless — some upstream in a pipe stopped consuming stdout early. If it's reproducible in a specific stage, that's a bug — file an issue.
- `1` catch-all: read the last 20 lines of stderr; that's where the actual error will be.

---

## Driver failures (shepherd / orchestrate / upstream-port)

### `shepherd.sh` bailed mid-run — how to resume

Shepherd runs stages sequentially. If a stage fails, the issue stays in whatever state that stage moved it to, and `logs/escalations.log` records the terminal state.

To resume, you can either:

- Re-run the whole shepherd — the state guard skips already-completed stages
- Fix the underlying issue (spec, code, config), then `./scripts/shepherd.sh --no-tmux EXP-N` again — it picks up from the current Linear state

Combine with `BUREAU_DRY_RUN=1` first to preview what shepherd will do without mutating anything.

### `orchestrate.sh --execute` — one lane failed, do the others continue?

**Yes** — lanes run in independent worktrees, so a lane failure never blocks siblings. But: **serial chains** stop at the first failure. If `EXP-9 → EXP-12` is a chain and EXP-9's Build fails, EXP-12 stays in Triage until you fix EXP-9.

To recover:

```sh
# See lane status
./scripts/bureau-status.sh

# Rerun a specific lane
./scripts/shepherd.sh --no-tmux EXP-9 --worktree .worktrees/shepherd-lane-2
```

Or re-run the whole orchestrate with the same schedule — completed tickets are skipped, failed ones retry.

### `upstream-port.sh` exit 15 — test gate false-negative

Sometimes the test command passes locally but not inside the worktree — usually a service dependency (postgres, redis) or a missing env var.

- Tail the failing test names from the log
- If it's environment: set the needed env in `.env` and re-run
- If it's flaky: `BUREAU_UPSTREAM_PORT_TEST="cargo test --workspace --no-fail-fast -- --skip flaky_test_name" ./scripts/upstream-port.sh ...`

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

The safety net exists because GitHub's `mergeStateStatus == CLEAN` is async-cached — it can lie for 30-60 seconds after main advances. Bureau's gates catch that.

### Code-review hit `BUREAU_MAX_REVIEW_CYCLES` — what now?

`agents.max_review_cycles` (default 3) caps how many `REQUEST_CHANGES → Build → Build Review` round-trips before parking.

- Read the last review comment on the PR — it lists what the reviewer keeps flagging
- If it's a legit disagreement, take over manually and either merge past it or fix
- If the reviewer's wrong, tune `agents.code_review.model` to something with better judgment

Cycle count survives across sessions via Linear labels (`bureau-review-cycle-N`), so restarting the pipeline doesn't reset it.

### QA returned `NEEDS_HUMAN` verdict

QA parks a ticket with `needs-human` when it can't decide whether a failure is legitimate (test genuinely fails) or spurious (env issue, flaky test, missing service).

```sh
# See what QA saw
grep "$ISSUE" logs/events.jsonl | jq -r 'select(.event=="qa_verdict")'
```

Typically: pull the branch locally, run the tests yourself, and either fix the code or mark the test as skip/ignore with a rationale.

### Codex-stage-runner failed spuriously

Symptom: a stage routed to Codex (`agents.<stage>.runner: "codex"`) fails with weird errors — timeouts on network calls, missing git objects, "sandbox denied write" errors.

Root cause: only `code_review` / `spec_review` / `research` are safe to route to Codex. Codex's exec sandbox has no network listeners, no git-metadata writes, no trust-store. `qa` and `implement` need all of those and false-halt inside the sandbox.

Fix: route the stage back to Claude. `claude_cmd_for_stage()` fires a stderr warning when you set an unsafe stage to Codex — grep the pipeline log for that warning.

---

## Operational

### Agents pause and don't restart — session throttle triggered

If `session.usage_threshold_pct` is set (default 80) and a usage signal at `~/.bureau/session-usage.json` reports above threshold, the queue-loop pauses before starting a new work unit.

To confirm: `cat ~/.bureau/session-usage.json` (or wherever `BUREAU_USAGE_FILE` points). If `pct > threshold`, that's why.

**Emergency bypass:** `BUREAU_DISABLE_THROTTLE=1 ./scripts/queue-loop.sh ...` for the current process only. Doesn't touch `.bureau.json`.

**Signal source:** ClaudeWatch is the community-standard writer. Without a producer, the throttle silently disables (no signal = keep working). If you want stricter behaviour, set `session.pause_on_stale_data: true` — but only after you have a producer.

### Worktree collision — "branch already checked out at another worktree"

Git refuses to check out the same branch in two worktrees. `queue-loop.sh` calls `free_branch_from_other_worktrees` to avoid this, but manual worktree operations can strand the state.

```sh
git worktree list                     # see all worktrees
git worktree remove --force <path>    # if a worktree is defunct
```

### Rebase-pipeline aborted mid-rebase

`rebase-pipeline.sh` runs `git rebase origin/main`. If a conflict appears in a hunk the auto-resolver can't handle, the rebase aborts and the ticket gets `needs-human`.

Recovery:

```sh
cd .worktrees/queue-rebase
git status                            # should show "rebase in progress"
# Resolve conflicts manually, then:
git add -A && git rebase --continue
# Or abort and let someone else deal with it:
git rebase --abort
```

Then remove the `needs-human` label from the Linear issue.

### Using `BUREAU_DRY_RUN` to debug a specific stage

Fastest way to reproduce a stage's decision-making without side effects:

```sh
BUREAU_DRY_RUN=1 ./scripts/<stage>-pipeline.sh <ISSUE-ID>
```

Every mutation logs `DRY-RUN:` instead of executing. Very fast, safe to spam.

---

## Token-efficiency layers

### Implement parks `status=STUCK` on a ticket that's actually complete
Pre-`use_goal_loop` symptom: implement runs, sees the tests already ticked (e.g. QA wrote them in a prior cycle), self-reports `status=COMPLETE` with 0 commits, the cumulative override flips to STUCK. EXP-571 / EXP-624 history.

Fix: enable `/goal` loop. `jq '.agents.use_goal_loop = true' .bureau.json | sponge .bureau.json`. The branch-ahead commit check still catches truly empty COMPLETE claims, but bash no longer second-guesses a legitimate single-iter COMPLETE.

### `headroom: command not found` after `headroom_wrap: true`
The flag is read live; turning it on doesn't auto-install Headroom. Install on the host running the pipeline (typically the operator's local box, not the CI runner):

```sh
pip install "headroom-ai[all]"
headroom --version    # confirm
```

If `pip` resolves to a Python that doesn't have user-level installs on PATH, prefer `pip install --user` and add `~/.local/bin` to PATH, or use a venv. The pipeline's `claude_cmd_for_stage` doesn't probe — the first invocation surfaces the error.

### Caveman style leaked into a commit message or PR body
By design, caveman is scoped to per-stage review prose only — commit messages and PR titles/bodies stay in normal register. If you see telegraphic caveman-speak in commits or PRs, the leak path is via the per-stage prompt prefixes in `code-review-pipeline.sh` getting applied where they shouldn't.

Audit: `grep -n "caveman\|/caveman" templates/scripts/code-review-pipeline.sh templates/scripts/merge-pipeline.sh`. The prefix should only appear in review-prose construction sites. If it's in a commit-message or PR-body builder, that's the bug — file an upstream ticket and pin `caveman_level: "off"` until fixed.

### `/goal` is a no-op (turn loops forever or exits without evaluation)
Requires Claude Code v2.1.139+ locally. Check `claude --version`. On older builds the slash command is unrecognized and the prompt is treated as raw text — implement-pipeline still parses the JSON status block but the iter-loop replacement isn't actually happening.

Fix: upgrade Claude Code, OR flip `agents.use_goal_loop = false` to fall back to the bash for-loop (preserved verbatim for exactly this scenario).

---

## tmux

### `WARNING: Old 'bureau' session is still running`
Harmless. The script checks for a legacy session named literally `bureau` (no `-v2`). If you never ran the v1 pipeline, ignore it — or delete the check block at the top of `start-bureau-v2.sh`.

### tmux session already exists on startup
`start-bureau-v2.sh` kills its own session (`bureau-v2-<basename>`) before recreating it. If you see unrelated stale sessions:

```sh
tmux ls
tmux kill-session -t <name>
```

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
Run `claude` interactively once to refresh OAuth. The headless `claude -p` calls used by the pipeline can't refresh tokens themselves.

### Headless Claude calls hang or timeout
Usually a network issue. Confirm:

```sh
claude -p "say hi" --print --dangerously-skip-permissions
```

If that hangs, restart Claude Code or check your connection.

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
Pre-v0.7.5 speckit installed via slash commands. The new install lives at `.claude/skills/speckit-*/`. After running `/bureau-init --resync-speckit`, clean up:

```sh
rm .claude/commands/speckit.*.md
```

### Constitution missing after speckit resync
`--resync-speckit` backs up `.specify/memory/constitution.md` before re-running `specify init` and restores it after. If the file is still missing, look for `.specify/memory/constitution.md.bak`.

---

## See also

- [Configuration](configuration.md) — config keys + env vars
- [Exit codes](exit-codes.md) — what each failure code means
- [Recipes](recipes.md) — common config patterns
