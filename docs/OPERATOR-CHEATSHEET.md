# Bureau operator cheat sheet

## Current Codex task

Install Codex interfaces with `$bureau-init --target codex` or `both`. Then use `$bureau TEAM-123 through review` or the specific `spec`, `implement`, `qa`, and `review` operations. The current task performs the work using its selected model; it does not launch a nested Claude/Codex CLI.

Inspect `$bureau status` before preparing work. `$bureau resume RUN_ID` reads the saved branch, artifacts and explanation. Follow the configured state IDs and canonical `bureau-branch` marker. Preserve current changes and other worktrees. Review stops before merge by default.

The [app setup guide](../templates/skills/bureau/references/app-setup.md) provides worktree setup and terminal actions. Use `bash scripts/bureau-app.sh doctor` for read-only diagnostics and `bash scripts/bureau-app.sh test` for the real `repo.test_command`.

## One background unit

```sh
python3 scripts/bureau-doctor.py --mode background
bash scripts/bureau-tick.sh --no-merge
cat logs/bureau-tick.json
```

At most one eligible stage runs in a claimed disposable worker. A successful process does not itself mean a completed ticket. Read `outcome`: waiting, advanced, completed, stopped_for_review, paused, blocked, or failed.

For one named ticket across stages:

```sh
bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123
```

The review boundary exits 20, human attention/pause 25, cancelled tickets 26. These prevent serial execution from treating halted work as Done. A full merge-enabled run requires authorization and all existing CI/base checks.

Background code review compares the PR's fetched head against its actual target branch. For a dependent PR, this excludes changes already supplied by the parent PR. Both commits are pinned for the review; unavailable or invalid target metadata stops execution before provider calls. Retargeting the PR or advancing either remote branch reopens a saved review stop. Review approval does not bypass the separate merge gates.

## Continuous mode

For explicitly requested always-on operation:

```sh
bash scripts/start-bureau-v2.sh
tmux attach -t "bureau-v2-$(basename "$PWD")"
```

One supervised queue per enabled agent polls at `agents.poll_interval_minutes`. This legacy launcher can proceed through merge; use the bounded tick or `shepherd.sh --no-merge` when the requested boundary is review. The launcher requires tmux and the configured background providers; current app tasks do not.

Use `BUREAU_SESSION_NAME=nightshift bash scripts/start-bureau-v2.sh` for a custom name. Different repositories need disjoint Linear project scopes: ownership is shared between worktrees of one Git repository, not between separate clones. Select one active team per configuration.

To stop dispatch, run `python3 scripts/bureau-runtime.py pause`, inspect active runs and let their stages finish. Stop the selected tmux session only after accounting for its workers. Killing tmux alone does not prove that nested provider processes stopped.

## Dry-run preview

```sh
BUREAU_DRY_RUN=1 bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123
```

Queue/shepherd previews can read Linear but do not invoke creative work or reset worktrees. They do not qualify authentication for a model, project tests or an end-to-end ticket. Use a representative bounded live run after setup is coherent.

## Several background tickets

1. Select well-defined eligible tickets and inspect dependencies.
2. Predict concrete repository-relative paths in the current task, saving `tickets` and `predictions` arrays. Use `node scripts/bureau-schedule-cli.mjs input.json > schedule.json`. The installed Claude workflow uses the same partition core.
3. Resolve every `blocked` prediction; failed/missing predictions stay in the output. Inspect the proposed components before execution.
4. Run `bash scripts/orchestrate.sh --execute --schedule schedule.json --max-concurrent 3 --no-merge`. Each lane uses its own claimed disposable checkout. The shared Git history and merge gates still constrain concurrent changes.

A schedule contains `parallelSafe`, `serialChains`, `edges`, and `blocked`. The executor rejects duplicate IDs, malformed schedules and unresolved predictions. Predictions are advisory and can underestimate conflicts; ownership does not eliminate eventual merge conflicts.

## Supervision and recovery

- `python3 scripts/bureau-runtime.py pause` stops future dispatch; running stages finish. `unpause` resumes dispatch.
- `python3 scripts/bureau-runtime.py status` reports claims and compact runs. `resume RUN_ID` retrieves full saved context and current ticket state.
- A failed worker with local/unpublished progress loses its disposable registration. Inspect and resume it instead of resetting it.
- Use the [supervision guide](../templates/skills/bureau/references/supervision.md) for native app automation when explicitly requested. One tick per wakeup; unchanged outcomes remain quiet.
- `bash scripts/bureau-status.sh --cost` reports available CLI estimates. Configure `session.cost_tracking`; unknown cost is unavailable. Claude and Codex quota signals are independent.

## Provider selection

`agents.runner` selects the default background CLI; `agents.STAGE.runner` overrides it. `BUREAU_RUNNER_STAGE` is the environment override. Use provider-specific models and an actual `repo.test_command`. Code review/research default to read-only; implementation, QA and spec repair require their permitted write/test capabilities. The shell executor owns Codex commits and independent implementation/QA test runs.

Claude `/goal` and Headroom apply only to Claude. Codex uses the portable bounded implementation loop. Environment restrictions must be reported separately from code failures. See [provider runtime](provider-runtime.md), [exit codes](exit-codes.md), [migration](migration.md) and [acceptance](codex-acceptance.md).


## CI scaffolding

In the current assistant, request `$bureau-init --resync-ci` (Codex) or `/bureau-init --resync-ci` (Claude). Preview the proposed workflow and preserve an existing custom one unless replacement is authorized. The generated job uses GitHub-hosted `ubuntu-latest`; replace its example test command with the adopting repository's actual checks.

A self-hosted runner executing public fork-PR code exposes that host to untrusted code. Keep the hosted default for ordinary installation; a deliberate self-hosted setup needs an isolated host and an explicit fork-PR approval policy. Never check out an untrusted PR head in a privileged `pull_request_target` job.

## Token-efficiency toggles

These optional settings remain off by default and affect background calls. See [token efficiency](token-efficiency.md) and [configuration](configuration.md).

| Setting | Effect |
|---|---|
| `agents.use_goal_loop: true` | Uses Claude's `/goal` capability for implementation. Codex retains the portable bounded loop. Verify support in the selected Claude installation first. |
| `agents.headroom_wrap: true` | Wraps Claude invocations with `headroom wrap`; install the wrapper before enabling it. |
| `agents.caveman_level: "full"` | Requests compact review prose. Commits and PR descriptions retain normal wording. |

Changing a runtime flag does not install its optional tools or rewrite project instructions. Roll back through `--update` or a reviewed configuration edit; asset resync is separate.

## Needs-human recovery

Read the issue's latest blocker comment, the owning run and stage log:

```sh
python3 scripts/bureau-runtime.py status
rg 'ESCALATED' logs/escalations.log
rg 'TEAM-123' logs/events.jsonl
tail -100 logs/queue-code-review.log
```

Resolve the actual spec, code, configuration or environment problem. Verify project tests and required CI before removing the configured human label. A failed label update is not proof that the ticket is safely parked. Inspect any saved checkout and interrupted process groups before releasing ownership; do not reset it to make the next pick succeed. Once the blocker and ownership are reconciled, a bounded tick or `shepherd.sh --no-tmux --no-merge TEAM-123` can continue the selected work.

## Memory loop and status

Use `$bureau-learnings` in Codex or `/bureau-learnings` in Claude to turn events and review feedback into `LESSONS.proposed.md`. Incorporate accepted findings into the existing `LESSONS.md` and review the diff before committing. Spec and code-review prompts consume accepted lessons as advisory context; the command does not auto-commit them.

```sh
bash scripts/bureau-status.sh
bash scripts/bureau-status.sh --config
bash scripts/bureau-status.sh --cost
```

The status and cost views help inspect a run; unknown cost is unavailable, not zero. See [recipes](recipes.md) for upstream ports, budgets, scheduling and usage-signal setup.
