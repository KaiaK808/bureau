# Configuration reference

Every knob bureau-init exposes — what it controls, where it lives, and what the default is. Two surfaces:

- **`.bureau.json`** — written by `/bureau-init` to the target repo's root. Gitignored. UUIDs + agent toggles.
- **Environment variables** — runtime-only overrides read by the pipeline scripts. Not persisted; usually set inline (`BUREAU_DRY_RUN=1 ./scripts/queue-loop.sh ...`) or in `.env`.

Defaults are designed so a fresh `/bureau-init` produces a working pipeline with no further editing. Everything below is for fine-tuning.

---

## `.bureau.json`

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

### `agents`

Which pipelines run, how often they poll, how aggressive they are.

| Key | Type | Default | Notes |
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
| `agents.poll_interval_minutes` | number | `30` | Cron tick frequency. Lower (e.g. `5`) for active work, higher for background |
| `agents.workbench_panes` | number | `2` | Number of interactive `claude` panes spawned in the workbench tmux window |
| `agents.max_review_cycles` | number | `3` | Code-review re-iterations before the agent gives up and applies `needs-human` |
| `agents.code_review_sampling_threshold` | number | `500` | Diff line-count above which code-review switches to sampling mode. Tune up for repos with strong CI |
| `agents.max_concurrent_issues` | number | `0` | Repo-wide cap on issues in flight. `0` = unlimited (default). `1` = single-flight (drain end-to-end before next Spec). See [recipes](recipes.md#single-flight-pipeline) |
| `agents.merge_strategy` | string | `"squash"` | One of `squash`, `merge`, `rebase`. Validated at config-load — invalid values fall back to `squash` with a warning |
| `agents.merge_require_green_ci` | bool | `true` | Bureau-enforced "all check-runs on PR head SHA must be completed + green" gate, independent of GitHub's `mergeStateStatus`. Catches the "no required-checks rule configured" hole where CLEAN passes with red CI. Set false only for repos genuinely without CI (docs-only, prototypes) |
| `agents.merge_require_up_to_date` | bool | `true` | Bureau-enforced "PR baseRefOid == origin/main HEAD" gate. Catches the async-cache race where `mergeStateStatus` still reads CLEAN after main has advanced. Set false only for repos using deliberate batch-merge workflows |
| `agents.merge_min_required_checks` | number | `1` | Minimum completed check-runs required on the PR head SHA before `merge_require_green_ci` will pass. Prevents a PR with zero registered workflows from passing vacuously. Set to `0` for repos with no CI at all (rare — prefer flipping `merge_require_green_ci` to `false` instead) |
| `agents.model` | string | unset | Default model for every stage. Falls through to the user's `claude` CLI default when absent |
| `agents.<stage>.model` | string | unset | Per-stage override. `<stage>` is one of `spec`, `spec_review`, `ux`, `copy`, `implement`, `qa`, `code_review`, `merge`. Resolution: stage → `agents.model` → CLI default. Set via `/bureau-init --update` (Models option group) or auto-prompted at the tail of `/bureau-init --resync-scripts` |

**Provider mixing constraint.** Anthropic models with reasoning enabled can only pair with other Anthropic models in the same context. Bureau is fine here — every pipeline run is a fresh `claude -p` subprocess, so cross-provider stage assignment works as long as each stage's full conversation stays within its provider.

### `repo`

| Key | Type | Default | Notes |
|---|---|---|---|
| `repo.branch_prefix` | string | `"feat"` | Prefix for spec branches (`feat/001-add-login`) |
| `repo.commit_prefix` | string | `""` | Optional prefix for commit messages (`[EXP] feat(login): ...`) |
| `repo.specs_dir` | string | `"specs"` | Directory where speckit writes specs — must match `.specify/`'s configured path |
| `repo.copy_voice_file` | path | unset | Required if `agents.copy: true`. Path to a markdown file describing voice/tone (e.g. `docs/voice.md`) |

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
| `BUREAU_DRY_RUN=1` | Short-circuits all Linear mutations, all comments, all label changes, all `git push`, all `gh pr create`. Logs the intent and returns success. Use to validate a fresh checkout against a real Linear team without polluting state |
| `BUREAU_SESSION_NAME` | Override the default tmux session name (`bureau-v2-<repo-basename>`) |

### Linear / external services

| Var | Required when | Notes |
|---|---|---|
| `LINEAR_API_KEY` | Always (agents) | Set in `.env`. The interactive `/bureau-init` works without it via MCP; the headless agents need direct REST access |
| `TELEGRAM_BOT_TOKEN` | Optional | Telegram bot for failure alerts. No-op when unset |
| `TELEGRAM_ALERT_CHAT_ID` | Optional | Chat/channel ID for alerts. Must be set alongside the token |

### Implement-pipeline retry loop

Env-only knobs (no `.bureau.json` equivalent). `implement-pipeline.sh` invokes Claude inside a bounded retry loop and parks the issue with `needs-human` if it doesn't reach `status: COMPLETE` within the budget.

| Var | Default | Notes |
|---|---|---|
| `BUREAU_IMPL_MAX_ITER` | `3` | Max Claude passes per tick. Each iter parses the JSON status block, pushes commits, and decides continue/stop |
| `BUREAU_IMPL_ITER_TIMEOUT` | `1800` | Per-iter wall-time cap (seconds). Uses `timeout` (Linux) or `gtimeout` (macOS via `brew install coreutils`). Degrades to cumulative-only with a WARN if neither is on PATH |
| `BUREAU_IMPL_TOTAL_TIMEOUT` | `5400` | Cumulative wall-time cap (seconds) across all iters in one tick. Hard upper bound on cost per issue per tick |

Defaults give ≤90 min worst case per tick before parking. Single-strike stuck detector: if an iter produces no `[X]` marks, no review fixes, AND no commits, the issue is parked immediately — Claude is spinning, not making progress.

Terminal states map to PR state + Linear:

| Status | PR | needs-human label | State move | `logs/escalations.log` |
|---|---|---|---|---|
| `COMPLETE` | flipped to ready (`gh pr ready`) | no | → QA / Build Review | no |
| `NEEDS_HUMAN` / `STUCK` / `CAP_TIME` / `PARTIAL` | draft | yes | stays in Build | yes |

### Token-efficiency flags (`.bureau.json` `agents.*`)

Three opt-in toggles that change implementation-loop control flow, prompt compression, and response style. All default OFF. See `docs/token-efficiency.md` for the concept-level explainer and the brainhuggers-cli pilot data; this section is the flag reference.

Live JSON read on every invocation (same pattern as `cost_tracking`), so flipping a flag mid-flight doesn't require a queue-loop restart.

| Flag | Type | Default | Effect |
|---|---|---|---|
| `agents.use_goal_loop` | bool | `false` | When true, `implement-pipeline.sh` drives via `claude -p "/goal CONDITION"` instead of the bash for-loop. Haiku evaluates the goal condition after every turn; `BUREAU_IMPL_MAX_ITER` becomes "stop after N turns" *inside* the goal condition rather than a bash bound; `BUREAU_IMPL_ITER_TIMEOUT` does not apply (turns end naturally, not on a wall-time cap). The stuck-detector tangle (EXP-573 / EXP-571 / EXP-624 / EXP-627) doesn't apply on this path. Requires Claude Code v2.1.139+ on the host. |
| `agents.headroom_wrap` | bool | `false` | When true, `claude_cmd_for_stage` prefixes every claude invocation with `headroom wrap`, so Headroom's compression pipeline sits between the script and Anthropic. Reversible (CCR) — Claude can call `headroom_retrieve` to fetch originals. Requires `headroom` on PATH (`pip install "headroom-ai[all]"`). Scoped to the claude backend only — the codex runner path is left alone. |
| `agents.caveman_level` | enum: `off`/`lite`/`full`/`ultra`/`wenyan` | `"off"` | `off` skips install entirely. The others trigger `npx skills@latest add JuliusBrussee/skills` at `/bureau-init` time (or Phase 6e on re-run) and apply `/caveman <level>` to review-prose-heavy stages only. Commit messages and PR titles/bodies stay in normal register. |

Env-var overrides follow the same `BUREAU_<FLAG>=1` pattern as `BUREAU_COST_TRACKING`: `BUREAU_USE_GOAL_LOOP=1`, `BUREAU_HEADROOM_WRAP=1`, `BUREAU_CAVEMAN_LEVEL=ultra`. Env wins over JSON when both are set.

Rollback: each layer is independently flippable. If something misbehaves, set the offending flag to `false` (or `"off"`) and the pipeline reverts to the prior code path on the next tick — no scripts to re-generate, no state migration.

### Per-stage model overrides (env shortcuts for `agents.<stage>.model`)

| Var | Stage | Resolution priority |
|---|---|---|
| `BUREAU_MODEL_DEFAULT` | all | Lower than per-stage |
| `BUREAU_MODEL_SPEC` | spec | Highest for that stage |
| `BUREAU_MODEL_SPEC_REVIEW` | spec_review | |
| `BUREAU_MODEL_UX` | ux | |
| `BUREAU_MODEL_COPY` | copy | |
| `BUREAU_MODEL_IMPLEMENT` | implement | |
| `BUREAU_MODEL_QA` | qa | |
| `BUREAU_MODEL_CODE_REVIEW` | code_review | |
| `BUREAU_MODEL_MERGE` | merge | |

These are read by `claude_cmd_for_stage()` in `bureau-config.sh`. The `.bureau.json` keys are the canonical surface; env vars are useful for one-off experiments (`BUREAU_MODEL_CODE_REVIEW=claude-haiku-4-5-20251001 ./scripts/queue-loop.sh code-review 5`).

### Supervisor

| Var | Default | Notes |
|---|---|---|
| `BUREAU_SUPERVISOR_MAX_CRASHES` | `5` | Override `supervisor.max_crashes` |
| `BUREAU_SUPERVISOR_STABILITY_WINDOW` | `3600` | Override `supervisor.stability_window` |

---

## Resolution order

For any value that exists in both `.bureau.json` and the environment:

1. Per-stage env var (e.g. `BUREAU_MODEL_IMPLEMENT`)
2. Stage key in `.bureau.json` (e.g. `agents.implement.model`)
3. Default env var (e.g. `BUREAU_MODEL_DEFAULT`)
4. Default key (e.g. `agents.model`)
5. CLI / shell default (no flag passed)

`bureau-config.sh` is the source of truth — when in doubt, grep it for `bureau_get` calls to see exact precedence.

---

## See also

- [Exit codes & alerts](exit-codes.md) — what each pipeline failure code means and how Telegram throttling works
- [Recipes](recipes.md) — common config patterns (single-flight, mixed models, dry-run)
- [Troubleshooting](troubleshooting.md) — when things break
