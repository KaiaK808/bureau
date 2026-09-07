# Background operations

Read this reference when choosing or updating background operation. Setup installs capabilities; it does not authorize launching workers, publishing changes or merging. Current Codex app tasks use the separate [app protocol](../docs/stage-protocol.md), not these disposable-worker drivers.

## Choose a driver

| Mode | Command | Behavior |
|---|---|---|
| Continuous queue | `bash scripts/start-bureau-v2.sh` | Supervised per-stage loops in tmux; configured polling and optional gated merging |
| Bounded tick | `bash scripts/bureau-tick.sh --no-merge` | Select and run at most one eligible stage; preserve a review boundary |
| Single ticket | `bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123` | Drive the named ticket in its claimed worker until completion or a stop; `--worktree DIR` selects a different worker path |
| Batch | `bash scripts/orchestrate.sh --execute --schedule schedule.json --max-concurrent 3 --no-merge` | Run independent lanes concurrently, serial chains in order; a nonzero result, including a review stop, halts that lane |
| Upstream port | `bash scripts/upstream-port.sh --sha COMMIT` or `--pr NUMBER` | Apply a selected upstream commit or merged PR, run configured build/tests and publish a port PR |

Use the actual ticket identifiers and reviewed schedule. The shared conflict scheduler produces `serialChains`, `parallelSafe` and any `blocked` work. The executor refuses unresolved blocked work; a prediction is not a substitute for reviewing dependencies and shared generated files. Start with `orchestrate.sh ... --dry-run` to inspect the planned commands without launching shepherds. See the [operator guide](../docs/OPERATOR-CHEATSHEET.md).

For upstream ports, explicitly configure `repo.upstream`, `repo.upstream_port.build_cmd`, `repo.upstream_port.test_cmd` and `repo.upstream_port.work_dir` for the adopting project. Do not copy another project's defaults. `--with-llm` optionally invokes the selected `upstream_port` provider once to resolve a conflict; the cost gate asks in a terminal and requires `--yes` for an authorized noninteractive invocation. The resulting PR is marked LLM-assisted. Porting changes the checkout and can push/create a PR; it is not a read-only review.

## Installed scripts

All runtime scripts come from the loaded skill's `templates/scripts/`, installed by `bureau_install.py assets --scope scripts`. Never reconstruct them from this inventory or assume a fixed skill installation path.

| Group | Files |
|---|---|
| Drivers and status | `start-bureau-v2.sh`, `start-agents.sh`, `shepherd.sh`, `orchestrate.sh`, `upstream-port.sh`, `bureau-status.sh` |
| Per-stage workers | `spec-pipeline.sh`, `spec-review-pipeline.sh`, `ux-pipeline.sh`, `copy-pipeline.sh`, `implement-pipeline.sh`, `qa-pipeline.sh`, `code-review-pipeline.sh`, `merge-pipeline.sh`, `rebase-pipeline.sh` |
| Polling and helpers | `queue-loop.sh`, `queue-loop-supervised.sh`, `bureau-config.sh`, `crosscheck-specs.sh`, `setup-merge-drivers.sh`, `grab-issue.sh`, `complete-issue.sh` |
| Shared app/background operation | `bureau-runtime.py`, `bureau-app.sh`, `bureau-doctor.py`, `bureau-worker.sh`, `bureau-tick.sh`, `bureau-supervision.py`, `bureau-monitor.py` |
| Provider and schedule adapters | `bureau-provider.py`, `codex-stage-runner.sh`, `bureau-schedule.mjs`, `bureau-schedule-cli.mjs`, plus the installed stage schemas |

The merge stage independently checks CI, the current PR base and other gates immediately before merging. Rebase remains opt-in because it force-pushes eligible Bureau-owned history. `setup-merge-drivers.sh` registers the local `ours` driver used by `.gitattributes`; it does not install arbitrary union/lockfile policies. `crosscheck-specs.sh` helps inspect spec/PR file overlap. `bureau-status.sh --config` shows installed settings; `--cost` reads optional legacy cost reports, not billed account totals.

## Optional settings

Preserve existing values during updates and add these only where useful. The [configuration reference](../docs/configuration.md) and [provider contract](../docs/provider-runtime.md) describe the full schema.

| Setting | Default / purpose |
|---|---|
| `agents.max_concurrent_issues` | `0` (unlimited); `1` gates new Spec admissions until in-flight issues drain |
| `agents.code_review_sampling_threshold` | `500` changed lines before reviewers receive sampling guidance |
| `agents.merge_min_required_checks` | `1`; retains protection against a vacuous green-CI result |
| `session.cost_tracking` | `false`; optional usage/cost evidence |
| `session.usage_threshold_pct` | `80`; pause according to the selected provider's available usage signal |
| `session.pause_on_stale_data` | `false`; opt into pausing on stale signals |
| `repo.path_prefix_strip` | Empty; optional normalization of a known project prefix in scheduled paths |

Single-flight admission is a Linear query; it does not coordinate unrelated clones or replace leases. The reverse stage priority applies to `queue-loop.sh all`, not to independent per-stage tmux loops. Multiple repositories need their own configuration and worker roots, distinct tmux session names (the launcher uses the repository basename), and intentionally separated Linear project scopes when they must not compete for tickets.

Provider routing uses `agents.runner`, `agents.<stage>.runner` or `BUREAU_RUNNER_<STAGE>`. Codex implementation and QA use the shared adapter and independent project-test gate; they are no longer restricted to review-shaped stages. Set the actual `repo.test_command` and qualify permissions, dependencies and model access before using them on real work. Keep legacy v1 generic Claude model fields intact; use `BUREAU_CODEX_MODEL_<STAGE>` or `agents.providers.codex.model` for Codex. `BUREAU_CODEX_MODEL_DEFAULT` is a lower-priority fallback. Inspect the real result with `python3 scripts/bureau-provider.py --stage STAGE --describe` in the launch environment; it does not source `.env` or authenticate.

Other supported runtime controls include `BUREAU_COST_TRACKING=1`, `BUREAU_FORCE_ALL_AGENTS=1` (bypass agent toggles only), and `BUREAU_DISABLE_THROTTLE=1` (bypass the usage throttle for this invocation). Use overrides deliberately; none grants merge permission or bypasses ownership checks. Stages with model settings include `research` and `upstream_port` as well as the usual pipeline stages. There is no shared-adapter `BUREAU_RUNNER_DEFAULT` override; select the default in `agents.runner`.

## Dry runs, drift and recovery

`orchestrate.sh --dry-run` previews its plan without dispatch. `BUREAU_DRY_RUN=1` is supported by shared queue/stage paths, but is not a universal no-mutation switch for every installed script. Reads and prerequisite checks may still run; use the specific driver's documented preview, especially for upstream ports. Installation previews are the separate `bureau_install.py` default and write nothing.

The launcher reports template differences, but upgrades use [resync](resync.md): inspect every customized file, preview the selected asset batch and retain intentional drift. Do not copy old scripts wholesale over a new runtime. Preserve interrupted worktrees and ownership records for inspection; a stopped process or old `.worktrees/` path is not permission to reset it. See [runtime patterns](runtime-patterns.md) and [stage recovery](../docs/stage-protocol.md).
