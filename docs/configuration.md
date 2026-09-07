# Configuration reference

Every knob bureau-init exposes — what it controls, where it lives, and what the default is. Two surfaces:

- **`.bureau.json`** — written by `/bureau-init` to the target repo's root. Gitignored. UUIDs + agent toggles.
- **Environment variables** — runtime-only overrides read by the pipeline scripts. Not persisted; usually set inline (`BUREAU_DRY_RUN=1 ./scripts/queue-loop.sh ...`) or in `.env`.

Setup captures the project-specific settings. Verify credentials, installed tools and real tests before running work. Current app tasks use the app-selected model; provider settings below select background CLIs.

---

## `.bureau.json`

### Version and validation

Version 1 remains compatible; version 2 makes the legacy `agents.runner: "claude"` default explicit. Migration preserves existing fields, adds `agents.model_compatibility: "v1"` to retain legacy provider model selection, and saves a private exact backup. `bureau-doctor.py` validates shape/version and reports effective settings without contacting providers. See [migration and rollback](migration.md).

### `linear`

| Key | Type | Required | Notes |
|---|---|---|---|
| `linear.teams[0].id` | UUID | yes | Linear team UUID |
| `linear.teams[0].key` | string | yes | Team key (e.g. `EXP`) — used in issue identifiers |
| `linear.teams[0].name` | string | yes | Display name; cosmetic only |
| `linear.teams[0].states.triage` | UUID | yes | Where eligible issues live before pickup |
| `linear.teams[0].states.spec` | UUID | yes | Spec writing |
| `linear.teams[0].states.spec_review` | UUID | yes | Spec validation |
| `linear.teams[0].states.design` | UUID | yes | UX (only required when `agents.ux: true`) |
| `linear.teams[0].states.build` | UUID | yes | Implementation |
| `linear.teams[0].states.build_review` | UUID | yes | Code review |
| `linear.teams[0].states.done` | UUID | yes | Terminal success state |
| `linear.teams[0].states.qa` | UUID | optional | Required if `agents.qa: true`; absent disables the QA state machine entirely |
| `linear.teams[0].states.copy` | UUID | optional | Required if `agents.copy: true` |
| `linear.teams[0].states.merge` | UUID | optional | Required if `agents.merge: true` (gated merge stage between Build Review and Done) |
| `linear.labels.lane2.id` | UUID | yes | Eligibility label (commonly `lane-2`) — only issues with this label enter the pipeline |
| `linear.labels.lane2.name` | string | yes | Label display name — `pick_issue` filters by name, so keep these in sync |
| `linear.labels.needs_human.id` | UUID | yes | Park label — applied on unrecoverable failure to take an issue out of the queue |
| `linear.labels.needs_ux.id` | UUID | yes | Routes from Spec Review → Design |
| `linear.labels.ai_implementable.id` | UUID | yes | Required on issues for stages from Build onwards |
| `linear.labels.needs_copy.name` | string | optional | Required if `agents.copy: true`; routes from Spec Review (or UX) → Copy |
| `linear.projects` | array | optional | List of project UUIDs to scope `pick_issue`. Empty array = unscoped (entire team) |

The runtime uses the first configured team. State IDs are authoritative; display names may be customized.

### `agents`

Which pipelines run, how often they poll, how aggressive they are.

**Default semantics.** An absent stage is disabled. A boolean controls the stage directly; an object such as `{"enabled":true,"runner":"codex"}` is enabled unless `enabled` is explicitly false. The setup defaults below describe the initial configuration, not a fallback that enables missing stages. Preserve existing choices during updates.

| Key | Type | Setup default | Notes |
|---|---|---|---|
| `agents.spec` | bool | `true` | Triage → Spec Review |
| `agents.spec_review` | bool | `true` | Spec Review → Build / Design / Copy |
| `agents.ux` | bool | `false` | Design → Build / Copy. Off by default |
| `agents.copy` | bool | `false` | Copy → Build. Off by default |
| `agents.implement` | bool | `true` | Build → Build Review (+ QA if enabled) |
| `agents.qa` | bool | `false` | Build → QA → Build Review. Off by default |
| `agents.code_review` | bool | `true` | Build Review → Done (or Merge if enabled) |
| `agents.merge` | bool | `false` | Gated merge after code-review APPROVE. Off by default |
| `agents.rebase` | bool | `false` | Force-pushes to remote — opt-in for that reason |
| `agents.poll_interval_minutes` | number | `30` | Queue polling interval. Lower (e.g. `5`) for active work, higher for background |
| `agents.workbench_panes` | number | `2` background / `0` app | Number of interactive provider panes in the tmux workbench; zero omits it |
| `agents.max_review_cycles` | number | `3` | Code-review re-iterations before the agent gives up and applies `needs-human` |
| `agents.code_review_sampling_threshold` | number | `500` | Diff line-count above which code-review switches to sampling mode. Tune up for repos with strong CI |
| `agents.max_concurrent_issues` | number | `0` | Repo-wide cap on issues in flight. `0` = unlimited (default). `1` = single-flight (drain end-to-end before next Spec). See [recipes](recipes.md#single-flight-pipeline) |
| `agents.merge_strategy` | string | `"squash"` | One of `squash`, `merge`, `rebase`. Validated at config-load — invalid values fall back to `squash` with a warning |
| `agents.merge_require_green_ci` | bool | `true` | Bureau-enforced "all check-runs on PR head SHA must be completed + green" gate, independent of GitHub's `mergeStateStatus`. Catches the "no required-checks rule configured" hole where CLEAN passes with red CI. Set false only for repos genuinely without CI (docs-only, prototypes) |
| `agents.merge_require_up_to_date` | bool | `true` | Bureau-enforced "PR baseRefOid == origin/main HEAD" gate. Catches the async-cache race where `mergeStateStatus` still reads CLEAN after main has advanced. Set false only for repos using deliberate batch-merge workflows |
| `agents.merge_min_required_checks` | number | `1` | Minimum completed check-runs required on the PR head SHA before `merge_require_green_ci` will pass. Prevents a PR with zero registered workflows from passing vacuously. Set to `0` for repos with no CI at all (rare — prefer flipping `merge_require_green_ci` to `false` instead) |
| `agents.model` | string | unset | Generic default belonging to `agents.runner`; ignored for Codex under v1 model compatibility and not reused by a stage assigned to another provider |
| `agents.<stage>.model` | string | unset | Generic per-stage override; ignored for Codex under v1 model compatibility. Under v2 semantics it belongs to the stage's selected runner. Provider and environment settings also participate in resolution; see [exact precedence](provider-runtime.md). Set through the compatibility-aware Models group in `/bureau-init --update` |

Stage settings apply to `spec`, `spec_review`, `ux`, `copy`, `implement`, `qa`, `code_review`, `merge` and `rebase`. Provider helpers also accept `research`, `upstream_port` and `upstream_summary` when invoked by their callers. Legacy string switches are preserved by migration; prefer booleans or objects for new settings.

Version 1 configurations (including an absent version) and migrated configurations retaining `agents.model_compatibility: "v1"` preserve generic model fields as Claude settings. For Codex, use `agents.providers.codex.model` or a provider-specific stage environment override such as `BUREAU_CODEX_MODEL_IMPLEMENT`. A schema migration does not opt into v2 model semantics. Review the selected runners and generic fields before explicitly removing the compatibility marker from a version 2 config or setting it to `"v2"`.

| Provider key | Default | Purpose |
|---|---|---|
| `agents.runner` | `claude` | Default background provider |
| `agents.<stage>.runner` | inherited | Stage-specific `claude` or `codex` |
| `agents.providers.<provider>.model` | CLI default | Provider-specific model |
| `agents.<stage>.model` | inherited | Generic stage model; Claude-only under v1 compatibility, selected runner under v2 semantics |
| `agents.model_compatibility` | based on schema version | `v1` preserves legacy model meaning; migration retains it until explicitly changed |
| `agents.<stage>.reasoning_effort` | unset | Provider/model-supported reasoning value; provider defaults also supported |
| `agents.<stage>.sandbox` | stage-dependent | Codex `read-only` or `workspace-write`; provider defaults also supported |
| `agents.<stage>.timeout_seconds` | 900 | Adapter timeout, also configurable by provider |
| `agents.workbench_runner` | default runner | Interactive bench provider; zero panes omits it |
| `agents.upstream_summary` | false | Optional read-only upstream summary model pass |
| `session.cost_tracking` | false | Persist available token usage and CLI cost estimates |
| `session.usage_threshold_pct` | 80 | Pause at/above the available provider usage signal |
| `session.pause_on_stale_data` | false | Honor old usage signals when explicitly selected |

Read [provider runtime](provider-runtime.md) for exact model precedence, sandbox capabilities and structured results. Code review/research default to read-only; spec-review/implementation/QA can write within the permitted workspace. The executor runs actual implementation/QA tests separately. Unknown runners, malformed results and denied permissions are errors, never automatic fallback to Claude.

### `session`

Cost logging is opt-in. Usage throttling uses an operator-provided signal for the selected provider; no signal means unknown usage and dispatch proceeds.

| Key | Type | Default | Notes |
|---|---|---|---|
| `session.cost_tracking` | bool | `false` | Enables available provider token and estimated-cost records at `${BUREAU_COST_DIR:-~/.bureau/cost}/<issue>.jsonl`. Report via `scripts/bureau-status.sh --cost`; missing cost stays unavailable. Env override: `BUREAU_COST_TRACKING=1` |
| `session.usage_threshold_pct` | number | `80` | Pauses before a new work unit when the selected provider's reported usage is at or above this percentage. Needs an external signal producer; Claude and Codex signals are separate |
| `session.pause_on_stale_data` | bool | `false` | When true, continue applying the threshold to a signal older than five minutes. False ignores stale signals. Missing signals never cause a pause |

### `repo`

| Key | Type | Default | Notes |
|---|---|---|---|
| `repo.branch_prefix` | string | `"feat"` | Prefix for spec branches (`feat/001-add-login`) |
| `repo.commit_prefix` | string | `""` | Optional prefix for commit messages (`[EXP] feat(login): ...`) |
| `repo.specs_dir` | string | `"specs"` | Directory where speckit writes specs — must match `.specify/`'s configured path |
| `repo.test_command` | string | unset | Actual project tests; required for Codex implementation completion and the app tests action |
| `repo.copy_voice_file` | path | unset | Required if `agents.copy: true`. Path to a markdown file describing voice/tone (e.g. `docs/voice.md`) |
| `repo.upstream` | string | `"ultraworkers/claw-code"` | GitHub `owner/name` for `upstream-port.sh` cherry-picks. Env override: `BUREAU_UPSTREAM_REPO` |
| `repo.upstream_port.build_cmd` | string | `"cargo build --release -p brainhuggers-cli"` | Shell command run inside `work_dir` after `git apply` succeeds. Non-zero exit → exit code 14. Env override: `BUREAU_UPSTREAM_PORT_BUILD` |
| `repo.upstream_port.test_cmd` | string | `"cargo test --workspace --no-fail-fast"` | Shell command for post-build tests. Non-zero exit → exit code 15. Env override: `BUREAU_UPSTREAM_PORT_TEST` |
| `repo.upstream_port.work_dir` | string | `"${SCRIPT_DIR}/../rust"` | Working directory for build + test commands. Env override: `BUREAU_UPSTREAM_PORT_WORK_DIR` |
| `repo.path_prefix_strip` | string | `""` | Prefix stripped by `crosscheck-specs.sh` when comparing planned spec paths against in-flight PR file paths. Useful when specs reference paths with a repo-name prefix (`brainhuggers-bureau/`, `packages/foo/`). Env override: `BUREAU_PATH_PREFIX_STRIP` |

### `supervisor`

| Key | Type | Default | Notes |
|---|---|---|---|
| `supervisor.max_crashes` | number | `5` | Consecutive crashes before the supervisor gives up and fires a Telegram alert. Read from env `BUREAU_SUPERVISOR_MAX_CRASHES` if set |
| `supervisor.stability_window` | number | `3600` | Seconds of clean runtime before the crash counter resets. Read from env `BUREAU_SUPERVISOR_STABILITY_WINDOW` if set |

---

## Environment variables

### Runtime mode

| Var | Effect |
|---|---|
| `BUREAU_DRY_RUN=1` | Queue/shepherd previews read eligible work but do not launch creative stages or reset workers. Pipeline entry points return before stage work. This is a dispatch preview, not model/test qualification |
| `BUREAU_SESSION_NAME` | Override the default tmux session name (`bureau-v2-<repo-basename>`) |
| `BUREAU_SESSION` | tmux session selected by the launcher/status interface. The event helper records fields explicitly supplied by its caller; this variable does not automatically tag every event |
| `BUREAU_FORCE_ALL_AGENTS=1` | Bypasses `agent_enabled()` — every agent's queue-loop runs regardless of `.bureau.json` toggles. Useful when driving `shepherd.sh` end-to-end against a repo with agents intentionally disabled for cron |
| `BUREAU_DISABLE_THROTTLE=1` | Skips the usage guard for this process; does not change its threshold or `.bureau.json`. Provider quota errors can still stop work |
| `BUREAU_MAX_CONCURRENT` | Default lane cap for `orchestrate.sh` (3 when absent); `--max-concurrent` overrides it. This does not change the separate `agents.max_concurrent_issues` ticket cap |

### Linear / external services

| Var | Required when | Notes |
|---|---|---|
| `LINEAR_API_KEY` | Always (agents) | Set in `.env`. The interactive `/bureau-init` works without it via MCP; the headless agents need direct GraphQL access |
| `TELEGRAM_BOT_TOKEN` | Optional | Telegram bot for failure alerts. No-op when unset |
| `TELEGRAM_ALERT_CHAT_ID` | Optional | Chat/channel ID for alerts. Must be set alongside the token |

### Implement-pipeline retry loop

Env-only knobs (no `.bureau.json` equivalent). `implement-pipeline.sh` invokes the selected provider inside a bounded retry loop and parks the issue with `needs-human` if it doesn't reach `status: COMPLETE` within the budget.

| Var | Default | Notes |
|---|---|---|
| `BUREAU_IMPL_MAX_ITER` | `3` | Max provider passes per tick. Each iter parses the JSON status block, pushes commits, and decides continue/stop |
| `BUREAU_IMPL_ITER_TIMEOUT` | `1800` | Per-iteration provider wall-time cap in seconds, limited by the remaining total budget and enforced by the Python adapter for both providers |
| `BUREAU_IMPL_TOTAL_TIMEOUT` | `5400` | Budget in seconds for the implementation provider loop. Independent executor tests and publication can add time after it; this is not a monetary cap |

The default provider-loop budget is 90 minutes. An unproductive first `PARTIAL` iteration can become `STUCK`; prior productive iterations and legitimate `COMPLETE` results have separate handling. The executor checks Git evidence and, for Codex completion, the configured project tests. A provider's success text alone cannot complete the stage.

Terminal states map to PR state + Linear:

| Status | PR | needs-human label | State move | `logs/escalations.log` |
|---|---|---|---|---|
| `COMPLETE` | flipped to ready (`gh pr ready`) | no | → QA / Build Review | no |
| `NEEDS_HUMAN` / `STUCK` / `CAP_TIME` / `PARTIAL` | draft | yes | stays in Build | yes |

### Token-efficiency flags (`.bureau.json` `agents.*`)

Three opt-in toggles change background implementation control flow, prompt compression and response style. All default off. See [token efficiency](token-efficiency.md) for the historical rationale; provider support and actual measurements must be checked for the adopting repository.

Provider calls read these settings at invocation time. Change them between work units so a stage does not change policy partway through its run.

| Flag | Type | Default | Effect |
|---|---|---|---|
| `agents.use_goal_loop` | bool | `false` | Claude-only `/goal` implementation path, requiring a compatible Claude installation. It uses `BUREAU_IMPL_TOTAL_TIMEOUT`; Codex always uses the portable bounded loop |
| `agents.headroom_wrap` | bool | `false` | Wrap Claude calls with `headroom wrap`; install the optional wrapper first. Codex calls are unaffected |
| `agents.caveman_level` | enum | `"off"` | Compact review-prose preference (`off`, `lite`, `full`, `ultra`, `wenyan`). Optional skill installation is a separate setup choice; commits and PR descriptions remain normal |

Env-var overrides follow the same `BUREAU_<FLAG>=1` pattern as `BUREAU_COST_TRACKING`: `BUREAU_USE_GOAL_LOOP=1`, `BUREAU_HEADROOM_WRAP=1`, `BUREAU_CAVEMAN_LEVEL=ultra`. Env wins over JSON when both are set.

Rollback: each layer is independently flippable. If something misbehaves, set the offending flag to `false` (or `"off"`) and the pipeline reverts to the prior code path on the next tick — no scripts to re-generate, no state migration.

### Per-stage model overrides (env shortcuts for `agents.<stage>.model`)

| Var | Stage | Resolution priority |
|---|---|---|
| `BUREAU_MODEL_DEFAULT` | Claude stages | Legacy fallback after configured models and provider defaults |
| `BUREAU_MODEL_SPEC` | spec | Below provider-specific stage override; ignored for Codex under v1 compatibility |
| `BUREAU_MODEL_SPEC_REVIEW` | spec_review | |
| `BUREAU_MODEL_UX` | ux | |
| `BUREAU_MODEL_COPY` | copy | |
| `BUREAU_MODEL_IMPLEMENT` | implement | |
| `BUREAU_MODEL_QA` | qa | |
| `BUREAU_MODEL_CODE_REVIEW` | code_review | |
| `BUREAU_MODEL_MERGE` | merge | |
| `BUREAU_MODEL_RESEARCH` | research | |
| `BUREAU_MODEL_UPSTREAM_PORT` | upstream_port | |

These are read by the provider adapter; provider-specific overrides take precedence. Under v1 model compatibility, generic overrides belong to Claude and are ignored for Codex. The `.bureau.json` keys are the canonical surface; env vars are useful for one-off experiments (`BUREAU_MODEL_CODE_REVIEW=claude-haiku-4-5-20251001 ./scripts/queue-loop.sh code-review 5`).

### Backend routing (env shortcuts for `agents.<stage>.runner`)

| Var | Effect |
|---|---|
| `BUREAU_RUNNER_<STAGE>` | Override per-stage runner with `codex` or `claude`, e.g. `BUREAU_RUNNER_CODE_REVIEW=codex`. Wins over stage/default JSON |
| `BUREAU_CODEX_MODEL_<STAGE>` | Provider-specific stage model for Codex; use an identifier available to the account. Takes precedence over generic model settings |
| `BUREAU_CODEX_MODEL_DEFAULT` | Codex fallback after stage and provider JSON model settings |
| `BUREAU_CLAUDE_MODEL_<STAGE>` / `BUREAU_CLAUDE_MODEL_DEFAULT` | Equivalent provider-specific Claude overrides |

There is no `BUREAU_RUNNER_DEFAULT` override in the current adapter. Set `agents.runner` for the default provider.

### Cost tracking

| Var | Default | Notes |
|---|---|---|
| `BUREAU_COST_TRACKING=1` | (off) | Equivalent to `session.cost_tracking: true` — enable without editing `.bureau.json` |
| `BUREAU_COST_DIR` | `~/.bureau/cost` | Where per-issue cost JSONL logs land. Legacy `~/.brainhuggers/bureau-cost` still works if set explicitly |
| `BUREAU_USAGE_FILE` | `~/.bureau/session-usage.json` | Signal file the throttle reads. Legacy `BRAINHUGGERS_USAGE_FILE` honoured as a third-rung fallback |

### Upstream-port

Env overrides for the `repo.upstream_port.*` config family. Set inline when running `upstream-port.sh` against a repo whose defaults don't match.

| Var | Overrides |
|---|---|
| `BUREAU_UPSTREAM_REPO` | `repo.upstream` |
| `BUREAU_UPSTREAM_PORT_BUILD` | `repo.upstream_port.build_cmd` |
| `BUREAU_UPSTREAM_PORT_TEST` | `repo.upstream_port.test_cmd` |
| `BUREAU_UPSTREAM_PORT_WORK_DIR` | `repo.upstream_port.work_dir` |
| `BUREAU_PATH_PREFIX_STRIP` | `repo.path_prefix_strip` |

### Code-review internals

| Var | Default | Notes |
|---|---|---|
| `BUREAU_REVIEW_MERGE_CAP_KB` | `60` | Tail-cap (KB) per specialist review before the merger prompt. Prevents ARG_MAX overflow when an individual review runs long (typical for codex-routed stages that emit heavy transcripts) |

### Miscellaneous

| Var | Default | Notes |
|---|---|---|
| `BUREAU_CONFIG` | discovered | Explicit config path; otherwise resolve the current checkout and primary worktree. Keep the trusted private config out of commits |
| `BUREAU_ENV_FILE` | caller-dependent | Explicit trusted environment file used by runtime helpers when needed; doctor/provider `--describe` do not source it |
| `BUREAU_SCRIPT_DIR` | derived from `$0` | Path to the target repo's `scripts/`. Auto-detected in normal use — set only when sourcing helpers from an unusual location |

`BUREAU_HOME` and `BUREAU_SPECKIT_VERSION` are not supported runtime overrides. Use the documented individual paths; the Spec Kit 0.7.5 pin lives in `scripts/bureau_install.py`.

### Supervisor

| Var | Default | Notes |
|---|---|---|
| `BUREAU_SUPERVISOR_MAX_CRASHES` | `5` | Override `supervisor.max_crashes` |
| `BUREAU_SUPERVISOR_STABILITY_WINDOW` | `3600` | Override `supervisor.stability_window` |

---

## Resolution and provider usage

The actual creative invocation uses `bureau-provider.py`. Runner precedence is per-stage `BUREAU_RUNNER_STAGE`, stage JSON, default JSON, then Claude. Model precedence is provider-specific stage environment, generic stage environment, stage JSON, provider JSON, generic default JSON only for its owning default runner, provider environment default, and the legacy Claude environment default. See [provider runtime](provider-runtime.md).

`BUREAU_REASONING_STAGE`, `BUREAU_SANDBOX_STAGE` and `BUREAU_STAGE_TIMEOUT` override the corresponding provider settings. `BUREAU_CONFIG` selects the explicit configuration file; otherwise the current checkout and primary worktree are checked. `BUREAU_NO_MERGE=1` preserves the review boundary in background code review/merge entry points.

`BUREAU_CODEX_USAGE_FILE` supplies an operator-provided Codex signal (`pct`, `reset_epoch`, `updated_epoch`). A shared `BUREAU_USAGE_FILE` is usable by Codex only when tagged `"provider":"codex"`. Claude retains its legacy/ClaudeWatch sources. No signal means unknown usage and dispatch proceeds; provider quota failures are classified separately. Bureau does not scrape private Codex account files. The app's account-usage tool is independent of CLI token evidence.

Cost logs keep available CLI estimates separate from actual billed dollars (`null`). Missing Codex costs are never reported as zero. `bureau-status.sh --cost` displays `unavailable` when a total includes unknown values.

## See also

- [Exit codes & alerts](exit-codes.md) — what each pipeline failure code means and how Telegram throttling works
- [Recipes](recipes.md) — common config patterns (single-flight, mixed models, dry-run)
- [Troubleshooting](troubleshooting.md) — when things break
