# Bureau v3.5.1 — Spec Review and UX fail closed on invalid results

**Stable release · 2026-10-29 · source tag `v3.5.1` · prepared 2026-10-28.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.5.1) is marked as latest and records the exact tagged commit. Tagging and publication remain separate maintainer operations. The previous stable release is [v3.5.0](release-notes-v3.5.0.md); its notes retain the earlier release history.

v3.5.1 is a patch release on v3.5.0. It contains only [#98](https://github.com/KaiaK808/bureau/pull/98), which validates Spec Review and UX results before routing and binds their cached overlap readiness to the validated result contract. No configuration keys and no exit codes are new. The existing exit `22` (`provider-or-result-error`) widens to these invalid results; see the [exit-code contract](exit-codes.md).

## What changed since v3.5.0

All behavior changes below are on by default after resync. The readiness changes apply when the existing overlap gate is enabled; this release keeps its configuration unchanged.

### Spec Review and UX validate their result contracts

Both background stages pass their new schemas through the shared provider adapter for Claude and Codex. Spec Review requires an exact, case-sensitive `review_status` of `PASS` or `FAIL`, `ui_work_needed` and a string `summary`. The UI field accepts booleans and the legacy strings `true`, `TRUE`, `yes`, `YES`, `false`, `FALSE`, `no`, `NO`, normalized to the existing UI/no-UI routes. UX requires an exact, case-sensitive `design_status` of `COMPLETE`, `INCOMPLETE` or `NEEDS_HUMAN` and a string `summary`.

The v3.5.0 prompt fields remain optional: Spec Review accepts integer `issues_found` and `issues_fixed` and a string array `remaining_issues`; UX accepts `components_net_new` with path and prop sketches, `components_reused` paths and `open_questions` with question `q` and boolean `blocking`. Prop sketches require `name`, `type` and a scalar-or-null `default`. The shared validator enforces declared type unions. Unknown extra fields are rejected, including within component, prop and question objects. Codex uses the existing strict wire projection and canonical validation; Claude uses the existing structured-output and transcript recovery rules. Stage prompts now request the required result fields, with review gaps in `summary` and design details, prop sketches and questions in `design.md`. Customized prompts can keep the declared optional fields. See [the result contracts](stage-protocol.md#spec-review-and-ux-results).

### Invalid results hold before publication

Missing, unknown, wrongly typed or schema-invalid results exit `22`, preserve stage edits and hold the ticket in its current state with `needs-human`. A failed label write leaves a durable local hold. The stages publish no success comment, ordinary push or Build/Design move. The shell also checks exact known statuses before routing. Valid results retain these routes:

| Result | Route |
|---|---|
| Spec Review `PASS`, UI needed | Design with `needs-ux` |
| Spec Review `PASS`, no UI needed | Build through overlap admission |
| Spec Review `FAIL` | Spec for rework |
| UX `COMPLETE` | Build through overlap admission |
| UX `INCOMPLETE` | Spec Review for rework |
| UX `NEEDS_HUMAN` | Remain in Design with the existing human hold |

The deterministic governance check can still force a valid Spec Review result to `FAIL`. Ticket withdrawal is checked first and retains precedence over result errors and human escalation.

### Overlap readiness requires the current validated contract

Spec Review/UX readiness now records `validated_status` (`PASS`/`COMPLETE`) and `result_contract_sha256`, the SHA-256 digest of the installed canonical schema, alongside the existing branch, HEAD, task, ticket and configuration identity. Missing identity, a different digest or a non-passing status invalidates and deletes the cached record. A pending overlap readiness record from v3.5.0 therefore causes one fresh provider call on the first resume after resync, including when its original result was `PASS` or `COMPLETE`.

An invalid fresh result holds with `22`. A fresh passing result records current readiness; subsequent overlap waits still return quiet `29` and retry admission without another provider call. Copy readiness and admission rules remain unchanged. No manual readiness migration is needed.

## New files

| File | Installation and purpose |
|---|---|
| `templates/scripts/bureau-spec-review.schema.json` | Installed as `scripts/bureau-spec-review.schema.json` by scripts resync; validates Spec Review results by default. |
| `templates/scripts/bureau-ux.schema.json` | Installed as `scripts/bureau-ux.schema.json` by the same resync; validates UX results by default. |
| `tests/judged_stage_results_test.py`, `tests/test_judged_stage_results.sh` | Repository regression fixtures and their shell entry point; use fake Claude/Codex CLIs and local Git. |

The existing common-Git `bureau/admission/v1.json` readiness entries gain the two identity fields above. This release adds no state store or configuration key.

## Upgrade an existing installation

**From v3.5.0:** stop all shepherds, queue loops, dispatchers and supervisors, preserve unfinished work and customized assets, and compare installed-script drift. Select tag `v3.5.1` in the source skill checkout and refresh skill discovery. Resync the **complete scripts and interfaces scopes together** (`--resync-interfaces --resync-scripts`) in each adopting repository, including both new schema files, then restart drivers together. Resolve customizations through [preview and resolve](migration.md#preview-and-resolve-asset-conflicts). `--update` changes configuration only. See [Upgrade to v3.5.1](migration.md#upgrade-to-v351) and the [source-update instructions](migration.md#select-the-source-release).

Check customized prompts against the declared result fields and expect one fresh provider call for each old waiting Spec Review/UX readiness record. Legacy `ui_work_needed` strings stay accepted. Run doctor and qualify a representative ticket before unattended dispatch.

**From earlier versions:** follow [Upgrade to v3.5](migration.md#upgrade-to-v35) and its earlier sections first, then the v3.5.1 steps above.

## Compatibility and rollback

The v3.5 exit-code vocabulary, configuration schemas, Spec Kit 1.2.2 pin and app prepare/finish protocol remain compatible. Exit `22` already exists; its use widens to invalid Spec Review/UX results. The new result schemas and stricter routing apply automatically. Partial script/schema updates are incompatible.

To roll back, stop every driver, select tag `v3.5.0` in the source skill checkout and restore its scripts and interfaces together, or restore the private backup ([rollback](migration.md#rollback)), then restart drivers together. Preserve unfinished work and common-Git admission state for reconciliation. Rollback restores v3.5.0's result handling and cannot undo commits, pushes or Linear/GitHub changes made in between.

## Validation

The maintainer's release validation record reports two Codex review rounds for [#98](https://github.com/KaiaK808/bureau/pull/98): round 1 requested three changes and round 2 approved. GitHub CI on #98 at `0ce1b2a` is green on all 10 shards ([run 40703173924](https://github.com/KaiaK808/bureau/actions/runs/40703173924)) after one re-run of a macOS shard. On the first attempt, a gate cancellation test in `tests/provider_adapter_test.py` failed on timing; it covers code #98 does not change and is the known limitation below. The changed tests passed locally on macOS outside the sandbox. Provider calls are faked in tests. No installation has run v3.5.1 before the release. Release preparation changes documentation only.

<!-- main CI result pending -->

See [current release status](releases.md#current-release-status). The main-branch CI result remains for the maintainer to record.

## Known limitations

On macOS CI, a gate cancellation test can intermittently lose the cancellation exit code `130`. A gate run is never falsely green because of this. The fix is planned for v3.6.0.
