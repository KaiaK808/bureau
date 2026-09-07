# Recipes

Configuration overlays and launch examples. Merge settings into the existing `.bureau.json`; preserve project IDs, credentials and unrelated choices. Examples that run background work use the review-only boundary.

---

## Single-flight pipeline

Process exactly one issue end-to-end before another enters Spec. Eliminates cross-issue rebase storms in active repos.

```json
{
  "agents": {
    "max_concurrent_issues": 1
  }
}
```

**When to use:** repos with > 3 simultaneous feature tickets where `origin/main` advances faster than the pipeline can drain. Symptom: every cron tick re-runs `merge_origin_main_or_abort` against a different main, conflicts pile up.

**When not to:** small repos where parallelism actually helps. The cap is a knob, not a default.

A single stuck issue (e.g. `needs-human` parking) does *not* deadlock the cap — `count_in_flight_issues` excludes parked issues.

---

## Mixed models per stage

Use a strong reasoning model for spec, a fast cheap model for spec-review, and an *adversarial* model for code-review (catches what the implementer missed).

```json
{
  "agents": {
    "model": "claude-sonnet-4-6",
    "spec":         { "model": "claude-opus-4-7" },
    "spec_review":  { "model": "claude-haiku-4-5-20251001" },
    "implement":    { "model": "claude-opus-4-7" },
    "code_review":  { "model": "claude-haiku-4-5-20251001" }
  }
}
```

These are illustrative Claude model identifiers; use models available to the selected account. Provider/environment overrides also participate in [model precedence](provider-runtime.md). Legacy v1 model fields retain their Claude meaning after migration.

For mixed providers, use a stage runner and provider-specific models. Example overlay (merge into the existing configuration):

```json
{"agents":{"runner":"claude","implement":{"enabled":true,"runner":"codex"},"qa":{"enabled":true,"runner":"codex"},"code_review":{"enabled":true,"runner":"claude"},"workbench_panes":0},"repo":{"test_command":"python3 -m unittest discover"}}
```

Set a real project test command. Leaving models unset uses each provider's CLI defaults. A generic Claude model does not leak into the Codex stages. Current app tasks use their selected model independently. See [provider runtime](provider-runtime.md) and [acceptance](codex-acceptance.md).

---

## Dry-run

Validate a fresh checkout against a real Linear team without polluting state. No Linear mutations, no comments, no PRs, no `git push`.

```sh
BUREAU_DRY_RUN=1 ./scripts/queue-loop.sh implement 1
```

Or pass the flag directly:

```sh
./scripts/queue-loop.sh all 5 --dry-run
```

Queue and shepherd dry-runs report candidate stages without invoking creative work or resetting worktrees. They can read Linear, so they still need appropriate credentials. A dry-run does not validate a model's output, install dependencies, run tests, or prove live workflow completion.

For current-task work, use `$bureau TEAM-123 through review`. For a single background unit, use `bash scripts/bureau-tick.sh --no-merge`. See the [operator guide](OPERATOR-CHEATSHEET.md).

---

## Drive one ticket end-to-end (shepherd)

Drive one selected ticket through its configured stages without starting continuous queues:

```sh
bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123
```

The default disposable checkout is `.worktrees/shepherd/`; `--worktree DIR` selects another worker path. Existing unregistered or unfinished checkouts are preserved and can refuse reuse. Inspect ownership before resuming; never point a disposable worker at an app checkout.

```sh
BUREAU_DRY_RUN=1 bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123
```

The preview reads state and reports the route without running the stages. In a live pass, exit 20 means the review boundary was reached, 25 means pause/human attention, and 26 means cancellation. None means the ticket was merged. Omitting `--no-merge` permits the existing merge path, so do so only for an authorized end-to-end merge run.

**When to use:** a representative acceptance ticket or deliberate recovery after resolving its blocker. For batches use orchestrate; for explicitly requested continuous work use `start-bureau-v2.sh`.

---

## Ship a batch (orchestrate + conflict-aware-schedule)

Predict file footprints, partition overlaps, inspect the schedule, then execute it. The current app task can prepare `input.json`; Claude's installed workflow uses the same partition core. Example shape (replace the tickets and paths with actual predictions):

```json
{
  "tickets": [{"ticket":"TEAM-7","title":"Update guide"},{"ticket":"TEAM-8","title":"Update parser"}],
  "predictions": [{"ticket":"TEAM-7","predicted_paths":["docs/guide.md"]},{"ticket":"TEAM-8","predicted_paths":["src/parser.py"]}]
}
```

```sh
node scripts/bureau-schedule-cli.mjs input.json > schedule.json
# Inspect parallelSafe, serialChains, edges and blocked before execution.
bash scripts/orchestrate.sh --execute --schedule schedule.json --max-concurrent 3 --no-merge
```

The planner exits 25 if predictions are missing or invalid; resolve every `blocked` entry before execution. `orchestrate.sh` does not have a `--plan` option. Each lane uses `.worktrees/shepherd-lane-N`; a stopped or failed ticket halts its serial chain while independent lanes can continue. Review-only execution therefore does not automatically drain a chain past an unmerged dependency.

Limit concurrency according to local build capacity and the selected providers' available quota. Separate checkouts isolate files, but shared Git state, inaccurate predictions and later merges can still conflict. See the [operator guide](OPERATOR-CHEATSHEET.md).

---

## Port from upstream (upstream-port)

Fast-path cherry-pick from a configured upstream — skips the shepherd ceremony for mechanical ports.

```jsonc
// .bureau.json
{
  "repo": {
    "upstream": "ultraworkers/claw-code",
    "upstream_port": {
      "build_cmd": "cargo build --release -p your-cli",
      "test_cmd":  "cargo test --workspace --no-fail-fast",
      "work_dir":  "rust"
    }
  }
}
```

```sh
./scripts/upstream-port.sh --sha 53953a8               # port one upstream commit
./scripts/upstream-port.sh --pr  3024                  # or a merged upstream PR
./scripts/upstream-port.sh --sha 53953a8 --with-llm    # LLM-assisted conflict resolution
```

`--with-llm` makes one bounded call through the configured `upstream_port` provider on a `git apply --3way` conflict. It is off by default; non-interactive use additionally requires `--yes`. Inspect the estimated work before opting in. The PR title carries `(LLM-assisted)`. Upstream PR-body summaries are a separate opt-in via `agents.upstream_summary`.

**When to use:** you maintain a fork of an actively developed upstream and want mechanical ports without the full shepherd pipeline.

**When not to:** the ports touch large files diverged materially from upstream. `--with-llm` helps here but eventually a real spec + shepherd run is cheaper than fighting the diff.

---

## Cost tracking + reporting

```jsonc
// .bureau.json
{
  "session": { "cost_tracking": true }
}
```

Provider calls with available usage evidence record token counts and CLI estimates in `~/.bureau/cost/<issue>.jsonl` (overridable with `BUREAU_COST_DIR`). Report:

```sh
./scripts/bureau-status.sh --cost
```

Prints a per-issue, per-stage summary. Unknown cost is `unavailable`, not zero, and CLI estimates are separate from actual billed dollars. Disabling the flag suppresses the optional cost records.

**When to use:** as adoption ramps. Pairs with the token-efficiency stack — turn `cost_tracking` on, measure the baseline, flip `use_goal_loop` / `caveman_level` / `headroom_wrap`, measure the delta.

**Env quick-toggle:** `BUREAU_COST_TRACKING=1` — same effect without editing JSON.

---

## Mixed provider — Codex on code_review

Route background review through Codex while keeping the default provider Claude:

```json
{
  "agents": {
    "runner": "claude",
    "code_review": { "enabled": true, "runner": "codex" }
  }
}
```

The review model inspects the pinned PR head against its actual target branch. The shell pipeline publishes the review and performs state routing; an approval alone never authorizes merge.

Codex-specific overrides work with both legacy and v2 model semantics:

```sh
: "${CODEX_REVIEW_MODEL:?Choose an available Codex model identifier}"
BUREAU_CODEX_MODEL_CODE_REVIEW="$CODEX_REVIEW_MODEL" \
  bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123
```

Use `python3 scripts/bureau-provider.py --stage code_review --describe` in the trusted launch environment to confirm resolution without invoking a model. Account for `.env` overrides; this command does not source that file.

Codex implementation and QA also use the portable adapter. The shell executor owns commits and independent tests; configure a real `repo.test_command` and qualify the required environment capabilities. Do not infer full unattended support from installation alone. A provider change can affect output quality and usage, so measure it on a bounded ticket before broader adoption.

---

## Session-usage throttle

Pause new work when the selected provider's supplied usage signal reaches its threshold. Bureau does not produce account-usage signals itself. Supply one from an operator-controlled producer; ClaudeWatch-compatible files are supported for Claude.

```json
{
  "session": { "usage_threshold_pct": 80, "pause_on_stale_data": false }
}
```

The default Claude signal is `~/.bureau/session-usage.json`, or `BUREAU_USAGE_FILE`. Its numeric timestamp fields use Unix seconds, updated by the producer:

```json
{ "provider": "claude", "pct": 74.3, "reset_epoch": 1788775200, "updated_epoch": 1788773400 }
```

For Codex, set `BUREAU_CODEX_USAGE_FILE`, or point `BUREAU_USAGE_FILE` at a signal tagged `"provider":"codex"`. Claude usage never throttles Codex.

When a signal is older than five minutes, `pause_on_stale_data: false` ignores it; `true` continues applying the configured percentage threshold to it. Missing signals mean unknown usage and allow dispatch in either mode. A bounded tick returns for a later invocation; continuous workers can wait until the signal clears. This guard does not prevent provider quota failures during an active call.

`BUREAU_DISABLE_THROTTLE=1` skips this guard for the current process without changing JSON. Use only as a deliberate override after inspecting the signal.

---

## Monitor escalations

Background escalation sites append a tab-separated audit record after successfully adding the human label. Inspect the stage result and issue comment as well; a failed label update is not evidence of durable parking. Tail the log:

```sh
tail -F logs/escalations.log
```

Line format:

```
2026-05-13T19:18:23Z<TAB>ESCALATED<TAB>EXP-402<TAB>code-review<TAB>cycle=3<TAB>reason="REQUEST_CHANGES exceeded max_review_cycles"<TAB>pr=56<TAB>branch=049-parliament-debate
```

Filter to today's escalations from a specific pipeline:

```sh
grep ESCALATED logs/escalations.log | grep "$(date -u +%Y-%m-%d)" | grep code-review
```

The matching JSON event also fires to `logs/events.jsonl` (so `/bureau-learnings` picks escalations up automatically). The TSV exists for monitors that prefer flat-text grep over jq.

Hooks fire at five sites: code-review (cycle-limit / merge-fail-after-approve / BLOCK), qa (NEEDS_HUMAN verdict), implement (retry-loop terminal). Logs only on `add_issue_label` success — Linear API hiccups don't produce phantom escalations.

---

## Tune the implement retry budget

`implement-pipeline.sh` runs up to `BUREAU_IMPL_MAX_ITER` provider passes per tick (default 3), each capped at `BUREAU_IMPL_ITER_TIMEOUT` seconds (default 1800), with `BUREAU_IMPL_TOTAL_TIMEOUT` (default 5400) bounding cumulative cost.

Aggressive (small batches, fast feedback):

```sh
BUREAU_IMPL_MAX_ITER=2
BUREAU_IMPL_ITER_TIMEOUT=600
BUREAU_IMPL_TOTAL_TIMEOUT=1800
```

Patient (long-running specs, fewer escalations):

```sh
BUREAU_IMPL_MAX_ITER=5
BUREAU_IMPL_ITER_TIMEOUT=2400
BUREAU_IMPL_TOTAL_TIMEOUT=10800
```

The stuck detector treats an unproductive first `PARTIAL` iteration differently from a later partial result after committed progress. Inspect the final status and Git evidence rather than assuming every zero-commit iteration is stuck.

The Python provider adapter enforces the remaining per-iteration bound for both providers on macOS and Linux. `gtimeout` is not required for this path. Independent project tests and publication can extend the overall stage beyond the provider-loop budget.

---

## Token-efficiency stack

Three optional settings address Claude implementation control flow (`use_goal_loop`), review wording (`caveman_level`) and Claude prompt compression (`headroom_wrap`). All start disabled; Codex retains the portable bounded implementation loop regardless of the goal flag. See [configuration](configuration.md) and the [historical rationale](token-efficiency.md).

Enable one change at a time through the configuration-only update flow, measure a representative ticket, then decide whether to keep it:

1. Verify `/goal` works in the selected Claude installation before enabling `agents.use_goal_loop`. The provider adapter still bounds the call with the total implementation timeout.
2. Select `agents.caveman_level` only when compact review prose is useful to the team. Installing an optional skill or rewriting project instructions is a separate setup choice; resync does not automatically compress `CLAUDE.md`.
3. Install the optional `headroom` wrapper before setting `agents.headroom_wrap`. The adapter reports a missing wrapper; it does not silently bypass it. Validate the installed tool and measure the resulting calls rather than assuming a particular compression ratio.

Use `session.cost_tracking` to retain available usage estimates, keeping unknown costs unavailable. Rollback is a reviewed setting change back to `false` or `"off"`; it needs no asset regeneration. Change configuration between stages, not during a live run.

---

## Telegram alerts

Get a message when a pipeline fails. No-op when the credentials aren't set, so dev environments stay quiet.

In the target repo's `.env`:

```sh
TELEGRAM_BOT_TOKEN=123456:ABC-XYZ
TELEGRAM_ALERT_CHAT_ID=-100123456789
```

Throttling: max 1 alert per `(issue, class)` per hour, tracked at `/tmp/bureau-alerts.log`. Bypass by deleting the file.

See [exit codes](exit-codes.md) for the full alert classification.

---

## Multi-repo, side-by-side

Each repo gets its own tmux session, scoped by folder name. Size parallel activity to machine capacity and provider quotas:

```sh
cd ~/projects/app          && ./scripts/start-bureau-v2.sh   # → bureau-v2-app
cd ~/projects/api          && ./scripts/start-bureau-v2.sh   # → bureau-v2-api
tmux ls | grep bureau-v2-                                      # both listed
```

**Caveat:** use disjoint Linear project scopes for independent repositories/clones. Ownership is shared by Git worktrees of one repository, not by unrelated clones. These continuous launchers can merge; use the bounded review-only commands when that is the requested boundary.

---

## Override the session name

```sh
BUREAU_SESSION_NAME=nightshift ./scripts/start-bureau-v2.sh
tmux attach -t nightshift
```

Useful when you want a single named session you can find without remembering the basename.

---

## Sampling-mode threshold for code-review

Repos with mature CI / type-safety can review larger diffs exhaustively. Repos without CI need stricter sampling.

```json
{
  "agents": {
    "code_review_sampling_threshold": 1000
  }
}
```

Default is `500` lines. Above this, code-review specialists shift to sampling-by-class. Tune up for confidence in tooling, down for legacy code.

---

## Custom merge strategy

Repos with linear-history-via-rebase policies should opt out of squash:

```json
{
  "agents": {
    "merge_strategy": "rebase"
  }
}
```

Valid values: `squash` (default), `merge`, `rebase`. Anything else falls back to `squash` with a warning.

---

## Tighter supervisor

For long-running headless deployments, raise the give-up threshold so a transient flaky tick doesn't kill the pipeline:

```json
{
  "supervisor": {
    "max_crashes": 10,
    "stability_window": 7200
  }
}
```

Or override at launch:

```sh
BUREAU_SUPERVISOR_MAX_CRASHES=10 ./scripts/start-bureau-v2.sh
```

---

## Memory loop (logs → `LESSONS.md`)

Mine the pipeline's own runs for recurring failure modes and review-feedback patterns, then feed a human-curated summary back into future spec + code-review prompts.

### How it works

1. **`queue-loop.sh` emits events.** Every time the queue picks a real candidate, `run_script` writes two JSONL lines to `logs/events.jsonl` — one `stage_start`, one `stage_end` with `issue`, `branch`, `exit_code`, `class`, `duration_s`. Queue-empty ticks emit nothing.

   ```json
   {"ts":"2026-05-11T14:23:01Z","event":"stage_end","mode":"all","stage":"code-review-pipeline.sh","issue":"EXP-512","branch":"037-foo","exit_code":14,"class":"build-failed","duration_s":287}
   ```

   `logs/` is gitignored. `events.jsonl` is append-only — truncate it manually if it grows large; there's no auto-rotation in v1.

2. **`$bureau-learnings` or `/bureau-learnings` drafts `LESSONS.proposed.md`.** After a week of pipeline activity, run the Codex skill or Claude command in the target repo. It:
   - Filters events to the last 30 days.
   - Pulls Linear comments for failed and successfully-reviewed issues (via the Linear MCP).
   - Clusters failure modes by `(class, first-file-in-trace)`, review feedback by repeated 4–8-word n-grams.
   - Requires ≥3 distinct issues per finding — empty sections are explicitly labeled "below threshold," **not** filled with fabricated patterns.
   - Writes a draft `LESSONS.proposed.md` at the repo root with three sections (failure modes / review feedback / stage timing p50/p90).
   - **Never stages, commits, or pushes.**

3. **You curate.** Read `LESSONS.proposed.md`, incorporate accepted findings into the existing `LESSONS.md`, preserve prior lessons, then review and commit that diff. Anything you dismiss will be re-proposed by future runs if the pattern persists — that's a feature, not a bug.

4. **Pipelines read it back, advisory only.** `bureau-config.sh::build_lessons_context` reads `LESSONS.md` from cwd (the worktree root) and wraps it with a "Treat as advisory, not binding" preamble. The result is injected into:
   - `spec-pipeline.sh` Phase 1 (`speckit-specify`) — where decisions are first shaped.
   - `code-review-pipeline.sh` — into all three specialist prompts (correctness, security, performance).

   Other stages (`ux`, `copy`, `qa`, `merge`, `rebase`) deliberately do **not** include it. If the file is absent or whitespace-only, injection is a no-op — pipelines run unchanged.

### Why human-in-the-loop (not auto-applied)

Low-volume early data produces noisy clusters. Auto-applying would amplify garbage. Human curation gates the feedback loop and keeps signal high. The current assistant analyzes the bounded event/comment evidence and proposes the draft. No vector database or embedding service is required; the existing lessons remain unchanged until the maintainer incorporates the proposal.

### Schema fields

| Field | Type | Notes |
|---|---|---|
| `ts` | string | ISO-8601 UTC, auto-injected |
| `event` | string | `stage_start` \| `stage_end` |
| `mode` | string | `queue-loop.sh` mode (`spec`, `implement`, `all`, …) |
| `stage` | string | Pipeline script name (e.g. `spec-pipeline.sh`) |
| `issue` | string | Linear identifier (e.g. `EXP-512`) |
| `branch` | string | Spec branch from `<!-- bureau-branch: -->` marker (omitted if not yet resolved) |
| `exit_code` | number | Pipeline exit status (only on `stage_end`) |
| `class` | string | Mapping per [`docs/exit-codes.md`](exit-codes.md): `ok`, `queue-empty`, `linear-down`, `build-failed`, … |
| `duration_s` | number | Wall-time seconds between start and end (only on `stage_end`) |

To add your own fields from a pipeline script, call `emit_event "event=..." "key=value" ...` — values matching `^-?[0-9]+$` are auto-typed as JSON numbers. The helper is silent-on-failure (any error logs to stderr and returns 0) so it can never wedge a cron-driven pipeline.

---

## See also

- [Configuration](configuration.md) — full reference
- [Exit codes](exit-codes.md) — what each failure means
- [Troubleshooting](troubleshooting.md) — when things break
