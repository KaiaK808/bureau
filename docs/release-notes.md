# Bureau v3.6.0 — review isolation, evidence and bounded runs

**Release preparation · prepared 2026-11-02 · planned source tag `v3.6.0`.** v3.6.0 is a minor release on [v3.5.1](release-notes-v3.5.1.md). Tagging and GitHub Release publication follow the maintainer's go.

<!-- release: v3.6.0 tag date pending -->

<!-- release: v3.6.0 GitHub Release URL and publication state pending -->

The release includes [#96](https://github.com/KaiaK808/bureau/pull/96), [#97](https://github.com/KaiaK808/bureau/pull/97), [#100](https://github.com/KaiaK808/bureau/pull/100), [#101](https://github.com/KaiaK808/bureau/pull/101), [#102](https://github.com/KaiaK808/bureau/pull/102), [#103](https://github.com/KaiaK808/bureau/pull/103), [#104](https://github.com/KaiaK808/bureau/pull/104), [#105](https://github.com/KaiaK808/bureau/pull/105), [#106](https://github.com/KaiaK808/bureau/pull/106), [#107](https://github.com/KaiaK808/bureau/pull/107), [#108](https://github.com/KaiaK808/bureau/pull/108), [#109](https://github.com/KaiaK808/bureau/pull/109), [#110](https://github.com/KaiaK808/bureau/pull/110), [#111](https://github.com/KaiaK808/bureau/pull/111), the `actions/checkout` 7.0.1 dependency update [#8](https://github.com/KaiaK808/bureau/pull/8), and [#112](https://github.com/KaiaK808/bureau/pull/112) for review and QA configuration isolation. The v3 exit-code vocabulary, configuration schema versions and Spec Kit 1.2.2 pin stay in place.

## What changed since v3.5.1

### Review and QA configuration isolation

Code-review specialists, the fallback merger, the finding verifier, QA and their validation conflict repair exclude automatic project and local configuration. Claude starts with `--setting-sources user`, retaining the operator's user settings, authentication, endpoints, user instructions and plugins. Project agents, skills, commands, `.mcp.json` and hooks stay out through the adapter's isolation flags. Codex disables automatic project documents and hooks and ignores execpolicy rules while retaining Bureau's explicit sandbox, model and effort selections. Implement and other stages retain their existing configuration behavior.

Trusted instruction files, `scripts/bureau-stage.md` and their recursive `@` imports come from pinned PR-base Git blobs and travel in system text. Nested instructions retain their directory scope. Import examples in fenced blocks and inline code spans are ignored. The checkout stays in place. Unsafe paths or contents, imports beyond five levels, or a complete instruction block above 256 KiB stop with exit 24 before model launch. Direct review/QA adapter calls require a full pinned `BUREAU_PROVIDER_CONFIG_BASE` commit; a missing pin also exits 24. See [provider runtime](provider-runtime.md).

Changes to protected agent configuration produce deterministic `AGENT_CONFIG` annotations under their own review heading and `human_merge` evidence dispositions. Protected paths include instruction files, `.claude/**`, `.codex/**`, `.agents/**`, root `.mcp.json`, `scripts/bureau-stage.md` and imported dependencies. These annotations ask the human merger to inspect the configuration change, are excluded from fixes and verification, and never change the verdict.

QA pins the open PR's actual target before the relevant provider call, including optional validation conflict repair. A successful lookup that finds no open PR permits a fetched `origin/main` pin for an operator rerun or a branch before PR creation. Lookup, metadata and fetch errors stop with exit 18. Operators must provide working PR lookup and base-fetch access even when using the no-open-PR path.

### Evidence packet and advisory risk tier

The existing human-boundary acceptance brief gains stage verdicts and attempt IDs, CI check-run conclusion counts at its immutable HEAD, Git numstat and binary counts, files outside the committed task inventory, retained review rounds and cap, and per-stage/total tokens and estimated costs. Missing evidence is stated explicitly. CI collection failure renders `unavailable` and preserves the existing delivery, retry and deduplication path. Frozen PR/HEAD briefs retain their bodies; the extension has no backfill. See [the packet contract](stage-protocol.md#acceptance-briefs-at-human-merge-boundaries) and [ADR 0012](adr/0012-human-boundary-briefs-and-local-insights.md).

A mechanical `low`, `normal` or `high` risk tier accompanies those facts. Defaults classify docs/tests-only changes of at most 100 changed lines and five files as low when they contain no binary or sensitive path. A sensitive path or at least 1000 changed lines makes the tier high. Mandatory configuration, dependency, CI, environment and agent-path rules remain active when extra sensitive patterns are empty. The tier leaves routing, holds, verdicts and merge authority unchanged. Optional `agents.risk_tier` customizes thresholds and patterns; invalid settings exit 24 before stage mutations.

### Run budget and cooperative kill switch

Optional ticket budgets limit cumulative tokens, provider attempts and summed provider wall time across stages, retries, checker/verifier calls, worktrees and driver restarts sharing one Git directory. Both budget and trusted-progress policies default off. Unknown token usage stays null and, with a token limit, stops at the next safe checkpoint unless `allow_unknown_usage: true` permits it; known lower bounds still apply. Call slots are reserved atomically before paid work. Token/time allowances can overshoot through calls already admitted.

Budget exhaustion and opt-in progress stalls use the existing cooperative stop latch. Current writers finish and join; the next checkpoint prevents further ordinary provider work or publication, preserves unfinished work and releases ownership after writers finish. Confirmed ticket withdrawal keeps precedence. Stops latch before diagnostic output, so closed stderr cannot bypass preservation. Terminal reasons use the closed existing-exit mapping: `budget`, `timeout`, `watchdog`, `review_capped`, `human_hold`, `ticket_withdrawn` and `stalled`. See [the stop protocol](stage-protocol.md#ticket-budget-and-progress-stops).

Trusted progress requires a new contract-bound fact proved by an actual runtime-executed green gate at unchanged HEAD. Provider reports, commits, checkboxes and repeated green checks add no progress. The default enabled policy stalls after two completed work-stage attempts without a new fact; quiet waits and incomplete or held stages spend no turns. An empty enabled contract supplies no qualifying facts. Customized QA/review wrappers must use `bureau_run_gate`; `bureau_record_gate` remains observational.

Ticket ledgers, admitted-attempt histories and separate durable witnesses survive evidence pruning. Under an enabled policy, witnessed missing, empty or unreadable accounting holds with 25 / `budget`; other invalid mandatory accounting fails closed with 24. With both policies disabled, accounting failures warn without changing stage exits. Inspect and reconcile saved work and ownership before using the [explicit operator reset](configuration.md#ticket-run-budgets-and-trusted-progress). Deleting a ledger cannot renew an enabled allowance.

### Process-group termination and the opt-in outside-sandbox gate

The shared `bureau-process.py` helper handles termination for providers, gates, conflict helpers, dispatch, runtime and post-implement hooks. Cancellation latches before cleanup and retains exit 130 and its log; timeouts retain 124 and their consumed reservation. Failed termination signals remain uncertain unless group absence is proved. A bounded two-second probe/reap loop handles macOS zombie-only groups; an exited leader alone cannot establish safe cleanup. The v3.5.1 macOS gate-cancellation limitation is addressed by these runtime and harness changes in [#105](https://github.com/KaiaK808/bureau/pull/105) and [#106](https://github.com/KaiaK808/bureau/pull/106).

Durable unfinished-process holds are written before spawning. Uncertain termination keeps holds, leases, branch registration and checkout state, blocks further publication and makes a restarted dispatcher refuse with 25 / `human_hold`. Determined timeout/cancellation exits survive. Operators must stop and inspect writers, reconcile work and explicitly remove the relevant holds before restarting. Post-implement launch creates its hold before forking; EXIT masks repeated signals and joins the hook before any preservation push. An unfinished fifteen-second drain retains the hold. See [termination handling](provider-runtime.md#codex-implementation-sandbox-gate).

Opt-in `agents.providers.codex.gate_outside_sandbox: true` delegates the full configured gate after each successful Codex implementation pass and in QA's final phase to Bureau's existing filtered outside runner. Codex retains its editing sandbox and may run focused tests. Receipts and reservations bind the check to that pass, command and HEAD; final validation reuses it. Implementation receives the previous outside result in its next brief. Red completed implementation retains exit 14, red QA returns to Build, and independent blockers survive green tests. The default remains false. See [ADR 0013](adr/0013-codex-gate-delegation.md).

### Review merge in code with ADR citations

The three specialists still run in parallel. `bureau-review.py` merges their complete schema-valid JSON findings in code when summaries contain only neutral printable ASCII. It deduplicates by path, inclusive range and normalized title, retaining highest severity, distinct detail, source anchors, ADR references and per-variant actionability. Structured human holds force BLOCK. Prose, fences, malformed output, non-ASCII summaries or severity/hold summary signals select the existing model merger. Zero tallies and negated signals also select fallback. Every readable finding, gap, hold and severity floor survives fallback; unreadable or uncertain output requires at least REQUEST_CHANGES. Shell security, build and cycle rules retain the final decision. See [ADR 0011](adr/0011-independent-provider-review.md) and [the fixture comparison](review-merge-comparison.md).

Normal code merge uses three specialist calls; model fallback adds the verdict call. Optional source grounding defaults off; `required` adds BLOCK/25 for missing or invalid pinned base/head anchors. Parse/provider errors remain 22. The optional finding verifier is shadow-only and cannot change verdicts or floors. Claude admits up to eight tool-free checks with a 60-second limit each; Codex records unverifiable without launching a checker because its CLI lacks a supported all-tools-off switch. The revised review policy invalidates older reusable approvals.

Opt-in ADR context compares pinned PR base and head. Only accepted ADRs with identical paths and blob IDs are settled decisions, including for known-limit exclusions. Added, changed or removed ADRs are proposals under review; citations never give them settled authority. Findings can cite included IDs through `adr_refs`. The ADR-off path retains the legacy merger schema, wire projection and digest. The new [external review brief](../references/review-brief.md) applies the same authority rule.

Bounded PR/Linear comments carry a final run/stage/round/verdict footer. Implementation keeps the selected comment intact and appends that review's complete merged findings and dispositions only after matching completed evidence, receipts, digest and a reviewed head ancestral to its current HEAD. Older comments or unusable evidence retain comment-only feedback. Human and QA feedback keep their existing precedence. Normalized merged findings remain complete when the published comment is capped, and deferred/SKIP dispositions bind to the current specialist receipts in either merge mode.

### Test harness and CI robustness

Fixtures now wait for recorded dispatcher processes within a deadline, account for zombies, retain stable process identities and clean up owned descendants on failure. Shared absolute deadlines cover slow pipeline setup, execution and teardown; survivor checks still reject live owned processes or unreadable owned state. Mutation controls validate the mutated setup and its baseline. Outcome fixtures include the classifier helper and assert persisted stage outcomes. These are test changes with no adopting-repository resync action.

The public-reference check scans tracked and nonignored new files, including NUL-safe names, while excluding ignored bytecode. CI weights and fixtures account for long-running controls. The repository workflow updates `actions/checkout` from 7.0.0 to 7.0.1 in [#8](https://github.com/KaiaK808/bureau/pull/8). See [validation](#validation) for the supplied PR record and pending integrated checks.

### Onboarding and Spec Kit helpers

App finish now prints the exact release/prepare recovery commands after trusted configuration changes and requires a regenerated result with the new run ID. Documentation explains repository-local Spec Kit discovery for both hosts and the repository-root/explicit-`SKILL.md` workaround. Documentation-only tickets can use a reviewed repository check selected through trusted `repo.test_command` before prepare and shared stages on the parent issue. There is no per-ticket test-command override. See [onboarding recipes](recipes.md#documentation-only-tickets) and [configuration-change recovery](troubleshooting.md#configuration-changed-since-prepare).

Disposable workers gain frozen, offline, lifecycle-script-free pnpm and Yarn Classic dependency restoration inside their own worktree, with workspace-aware stamps and filtered child environments. Provision the manager and cache in trusted setup and remove `node_modules` from worktree links. Missing tools/cache, conflicting lockfiles or `packageManager`, modern Yarn and all Yarn PnP layouts fail with 24. npm retains its existing path. See [Node repositories](configuration.md#node-repositories).

Spec Kit apply repairs execute bits on every safe regular `.sh` helper under `.specify/scripts/`, including restored helpers and partial initialization failures, without changing content. Restoration and mode repair are attempted independently; cleanup errors preserve the primary failure and make an otherwise successful apply fail. Preview renames `helper_mode_fixes` to `existing_helper_mode_drift`, reporting only existing non-executable helpers. The updated doctor identifies them. Run `--resync-speckit` to apply this repair; the 1.2.2 pin and customization guard remain.

### Architecture recipe documentation

The [architecture recipe](recipes.md#architecture-recipe-adrs-before-the-first-build-ticket) supplies copyable tickets for new and existing projects, ADR review tracking and an owner summary. New decisions stay proposed through review until the reviewed acceptance edit; derived records retain dated code evidence. Constitution reconciliation requires a separate owner-authorized ticket. Adapt the template and check script to the project. This recipe installs no new assets through `bureau-init`.

## New files

| File | Installation and purpose |
|---|---|
| `templates/scripts/bureau-review-config.py` | Installed as `scripts/bureau-review-config.py`; pinned trusted instructions and deterministic configuration-change annotations. Included with [#112](https://github.com/KaiaK808/bureau/pull/112). |
| `templates/scripts/bureau-review.py` | Complete specialist merge, fallback floors, source anchors and shadow-verifier preparation. |
| `templates/scripts/bureau-specialist.schema.json`, `bureau-finding-verifier.schema.json` | Specialist and optional checker contracts; install with the review helper and pipelines. |
| `templates/scripts/bureau-adr.py`, `bureau-review-adr.schema.json` | Bounded pinned ADR index and the enabled ADR result contract. |
| `templates/scripts/bureau-review-context.py` | Evidence-bound complete review feedback for implementation. |
| `templates/scripts/bureau-ticket.py` | Authoritative ticket budgets, admitted attempts, progress facts and operator inspection/reset. |
| `templates/scripts/bureau-process.py` | Shared bounded termination checks and durable unfinished-process holds. |
| `templates/scripts/bureau-worktree-deps.py` | Filtered offline pnpm/Yarn Classic dependency restoration. |
| `references/review-brief.md`, `docs/review-merge-comparison.md`, `docs/adr/0013-codex-gate-delegation.md` | External review brief, fixture comparison and delegation decision. |
| `docs/release-notes-v3.5.1.md`, `docs/site/release-notes-v3.5.1.html` | Archived previous-release notes and rendered manual page. |
| Review, process, ticket-budget, dependency and onboarding fixtures under `tests/` | Repository-only regression tests, negative controls and legacy review fixtures; no installed runtime dependency. |

All `templates/scripts/` additions install under `scripts/` through the complete scripts resync. New shared runtime records live below `<git common dir>/bureau/`:

| State | Retention and operator action |
|---|---|
| `tickets/ISSUE.json`, `tickets/ISSUE.attempts.json`, `ticket-witnesses/ISSUE.json` | Accounting, admitted identities and durable witnesses outside prunable evidence; preserve across cleanup and reset only through the documented operator command. |
| `process-holds/` | Write-ahead unfinished-process records; inspect writers and remove holds explicitly after reconciliation. |
| Existing `ticket-control/RUN_ID/stop-25` and dispatcher skip records | Reused for budget/progress stops; reconcile the persisted hold before retrying. |

One new record lives outside the shared runtime directory: the retained `merged-review.json` sits in the provider attempt's log directory, by default `logs/provider-runs/` next to `.bureau.json` (override with `BUREAU_PROVIDER_LOG_DIR`). It holds the complete review prose outside the metadata-only evidence store and is needed for complete feedback expansion.

## New configuration keys and defaults

No configuration schema migration is required. Optional policies remain disabled until selected; the acceptance packet and risk classification apply at existing human boundaries without an enable switch. See [configuration](configuration.md) for validation, complete pattern rules and progress contracts.

| Key | Default | Effect |
|---|---|---|
| `agents.ticket_budget.tokens` | off (absent/null/0) | Cumulative token allowance; positive integer enables it. |
| `agents.ticket_budget.provider_calls` | off (absent/null/0) | Cumulative admitted provider-attempt allowance. |
| `agents.ticket_budget.wall_seconds` | off (absent/null/0) | Summed provider wall time; positive finite seconds. |
| `agents.ticket_budget.allow_unknown_usage` | `false` | Explicitly permits unknown tokens while retaining null totals and known lower bounds. |
| `agents.ticket_progress.enabled` | `false` | Enables trusted-progress stops. |
| `agents.ticket_progress.max_non_improving_turns` | `2` | Completed work stages without a new qualified fact before a stall. |
| `agents.ticket_progress.contract` | `{}` | Gate-bound fact IDs; an empty enabled contract cannot prove progress. |
| `agents.risk_tier.low_max_changed_lines` | `100` | Inclusive low-tier line cap. |
| `agents.risk_tier.low_max_files` | `5` | Inclusive low-tier file cap. |
| `agents.risk_tier.high_min_changed_lines` | `1000` | Inclusive high-tier line threshold. |
| `agents.risk_tier.docs_patterns` | `["docs/*", "**/docs/*", "*.md", "*.rst"]` | Replaces the docs-path defaults when supplied. |
| `agents.risk_tier.test_patterns` | `["tests/*", "**/tests/*", "test/*", "**/test/*", "test_*.py", "*_test.py", "*.test.*", "*.spec.*"]` | Replaces the test-path defaults when supplied. |
| `agents.risk_tier.sensitive_patterns` | `[]` plus mandatory built-ins | Adds sensitive paths; built-ins remain mandatory. |
| `agents.review_grounding` | `off` | `required` blocks missing/invalid pinned source anchors. |
| `agents.finding_verifier.enabled` | `false` | Bounded shadow checks; verdicts and floors remain unchanged. |
| `agents.review_adr_context` | `false` | Enables the pinned ADR index and extended review schema. |
| `agents.review_adr_budget_bytes` | `4096` | Whole-context UTF-8 byte budget; 0 omits the index. |
| `repo.adr_dir` | `"docs/adr"` | Validated repository-relative ADR directory. |
| `agents.providers.codex.gate_outside_sandbox` | `false` | Delegates implementation/QA full gates to the existing outside runner. |

Existing `BUREAU_REVIEW_MERGE_CAP_KB` stays at 60 KiB by default and now applies only to the model fallback; the code merge reads complete structured reports before that cap. Existing `agents.parallel_lanes` still supplies both dispatch and batch concurrency limits. Budget/progress settings add no separate batch limit or new exit code.

## Upgrade an existing installation

**From v3.5.1:** stop all shepherds, queue loops, dispatchers and supervisors; preserve unfinished work, customized assets, user instructions, Spec Kit assets and the constitution. Compare installed-script drift before replacing assets. After the maintainer publishes the release, select tag `v3.6.0` in the source skill checkout and refresh discovery using [the source-update instructions](migration.md#select-the-source-release). Preview and resolve conflicts through [the migration guide](migration.md#preview-and-resolve-asset-conflicts).

Resync the **complete scripts and interfaces scopes together** in every adopting repository, including all new helpers and schemas; restart drivers only at the end, as the last step below. A running shell keeps its loaded code; partial helper/schema/pipeline updates are incompatible. Use the installation's existing target selection:

```text
/bureau-init --resync-interfaces --resync-scripts --target both
```

Use `$bureau-init` for the same scopes in Codex. `--update` changes configuration only. Run `--resync-speckit` to repair helper modes while keeping the 1.2.2 pin, customized content and active integration; update preview consumers to `existing_helper_mode_drift`. Interfaces/workflows retain their existing scope rules. See [Upgrade to v3.6](migration.md#upgrade-to-v36).

**Default changes to inspect:** review/QA configuration isolation and pinned trusted instructions, `AGENT_CONFIG` notes on agent-configuration changes (including an adopting-repository resync PR), QA's required successful PR lookup and base fetch, structured review merge/fallback, one fresh review for older approvals, complete evidence-backed implementation feedback, acceptance packets and process holds. A QA branch with no open PR must be confirmed by lookup before fetched `origin/main` is used. Direct adapter callers must supply the base pin. Update customized prompts/wrappers to the new specialist and trusted-context contracts.

**Optional choices:** leave ticket budgets, trusted progress, required source grounding, finding verification, ADR context and Codex gate delegation off until deliberately configured and qualified. Review the default risk patterns even without custom settings. Provision offline pnpm/Yarn Classic tools and caches before workers use them; keep `node_modules` out of worktree links. Inspect existing unfinished-process and `dispatch-termination.json` holds before restart. Preserve ticket accounting, witnesses, admission, numbering, checkpoint, evidence and dispatcher state; frozen briefs and legacy comment-only feedback receive no backfill. Run doctor, restart drivers together and qualify a representative ticket before unattended dispatch.

**Restart last:** after the resync, the setup above (customized wrappers, offline pnpm/Yarn Classic caches, `node_modules` out of worktree links) and the reconciliation of holds, run `python3 scripts/bureau-doctor.py --mode background`, then restart all drivers together. Enable optional keys only after that, and run doctor again after configuring them.

**From v3.4.1:** first apply the intervening requirements in the [v3.5.0 notes](release-notes-v3.5.0.md#upgrade-an-existing-installation) and [v3.5.1 notes](release-notes-v3.5.1.md#upgrade-an-existing-installation), then follow the v3.6.0 steps above. These include Spec Kit 1.2.2, coordinated exits 28/29, launcher overlap opt-in, changed manual-Merge capacity/review defaults, governance and state retention, plus the Spec Review/UX schemas and one fresh provider call for old waiting readiness records. From earlier versions follow [Upgrade to v3.4.1](migration.md#upgrade-to-v341) and its earlier sections first.

## Compatibility and rollback

Configuration versions 1 and 2, Bash 3.2+, Python 3.9+, the existing app prepare/finish protocol and the v3.5 exit vocabulary remain supported. The shared scheduler retains its Node 18+ requirement. Spec Kit stays pinned to 1.2.2. New optional settings need no migration. Automatic isolation requires provider flags accepted by the installed CLI; local fake-provider fixtures supply no live-CLI compatibility claim. Existing customized instructions, helpers and constitution content must survive the resync.

To roll back, stop every driver and reconcile all unfinished writers and holds before restarting an older runtime. Select tag `v3.5.1` and restore its scripts and interfaces together, or restore the private backup ([rollback](migration.md#rollback)); restore compatible configuration and customized assets. Retain the v3.6 accounting, witnesses, process holds and evidence separately for inspection because an older runtime cannot enforce the new policies. Rollback restores v3.5.1 review/configuration and termination behavior and cannot undo commits, pushes, comments, labels or state moves made in between.

## Validation

The maintainer's release validation record reports every PR reviewed by Codex before push within at most three rounds, with two recorded maintainer exceptions: [#108](https://github.com/KaiaK808/bureau/pull/108) took five rounds and [#112](https://github.com/KaiaK808/bureau/pull/112) took four. Each PR's full local suite ran on macOS outside any sandbox before push, with failures rerun serially. CI on each PR is green; several macOS shards needed one rerun of failed jobs on tests the change does not touch. Release preparation changes documentation, plus three test repairs found by the integrated suite: an import that an integration merge dropped from the evidence negative-control script is restored, the process-group implement replay anchors at the gate-context reset that the implementation-feedback change moved, and the ticket-budget adapter fixture pins a provider base, which review and QA calls now require.

<!-- release: integrated release branch full-suite result pending -->

<!-- release: integrated release branch CI result pending -->

<!-- release: main CI run and result pending -->

The integrated branch's suite and CI and the main-branch CI record remain pending. The documentation preparation uses `bash tests/test_docs.sh` and `bash tests/test_no_internal_refs.sh`. See [current release status](releases.md#current-release-status).

## Known limitations

The v3.5.1 cancellation-exit limitation is removed from the active list because cancellation now survives process cleanup errors. Uncertain termination still requires operator inspection and explicit hold removal; it cannot authorize a green gate, publication or ownership release. Cross-platform fixture coverage and green PR CI leave live adopting-runner qualification outstanding.

Isolation trusts the pinned base and the operator's retained user configuration. Instruction/import paths must be safe regular UTF-8 blobs within the repository; symlinks, submodules, escapes, excessive import depth and oversize context fail closed. Provider flags need live CLI qualification. Source grounding checks anchor identity; finding truth and reviewer quality remain unproved by fixture tests. ADR context is bounded prompt guidance and may omit records.

Structured review accepts at most 8 MiB per specialist and 1024 findings per role, with regular source blobs capped at 2 MiB. Shadow verification is capped at eight checks, 24 KiB request JSON and 8 KiB of whole pinned contracts, and has no enforced mode. Codex checks stay unverifiable without model launch. Model fallback retains its extra call. Large complete implementation feedback remains subject to the live provider's context window; an uncapped comment fallback can exceed a platform's publication limit.

Evidence packets are frozen observations. Retention can expire review rounds; CI snapshots cover check runs and exclude legacy commit statuses. Cost coverage can be partial or unavailable and uses labeled legacy-log fallbacks. Sensitive-path classification is conservative and may need project-specific additions. None of these observations replaces the merge gate.

When a fixture pipeline in `tests/file_overlap_test.py` is killed at its deadline, retained admission state can cause later readiness subcases to fail. The shared deadline adjustment leaves that cleanup issue open. Sandbox restrictions can limit native process-inspection checks.

Budgets/progress coordinate only within one common Git directory and only for adapter-admitted calls. Already admitted work can overshoot token/time limits. Outside gates retain the outer runner's filesystem/network access; environment filtering cannot conceal same-account readable files. A process that clears its inherited ownership marker and detaches before observation can evade discovery. Live CLI, model-quality, package-manager/cache and adopting-repository qualification remain operator checks. Modern Yarn and PnP require a separately reviewed dependency setup. Architecture examples require adaptation and install no recipe assets automatically.
