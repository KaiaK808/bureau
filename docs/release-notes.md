# Bureau v3.9.2 — Decision answers, merge approval binding and Trivy parsing

**Release preparation · 2026-11-24 · source tag `v3.9.2`.** v3.9.2 is a patch release on [v3.9.0](release-notes-v3.9.0.md). Tagging and GitHub Release publication remain separate maintainer operations.

The release includes [#165](https://github.com/KaiaK808/bureau/pull/165), [#166](https://github.com/KaiaK808/bureau/pull/166), [#167](https://github.com/KaiaK808/bureau/pull/167), [#168](https://github.com/KaiaK808/bureau/pull/168) and [#169](https://github.com/KaiaK808/bureau/pull/169) in first-parent merge order. All changes are merged. The [changelog](../CHANGELOG.md#392---2026-11-24) collects the four release fragments.

## What changed since v3.9.0

### Exact answers to implementation decision holds

For humans answering implementation questions, this changes what counts as an answer: only a later Linear reply starting with the exact `Answer: <hold-id>` line, followed by a nonblank body, answers the hold. Earlier prose and completion footers cannot release a new hold. Every matching decision and its questions reach implementation through retries and QA rework. CRLF and CR replies preserve their body, and decision waits release on the next poll or restart.

Admission covers dispatcher-launched Shepherd and rejects failed or empty startup route reads before ownership or labels. Dispatcher reconciliation retries read-only admission failures without Linear mutations. Ticks and read/move handlers preserve exit 24. Admission, overlap preflight and preparation share complete paginated canonical-branch resolution across Shepherd, dispatcher, queue and direct workers. Hold questions and app summaries lose private metadata before the runtime adds the ID and copyable instructions. A time cap before the first pass still labels and summarizes the halt.

### Merge approvals bound to the reviewed head and target branch

Both automatic merge routes bind `APPROVE` and `AUTO_APPROVE` to the reviewed head and target branch on both gate passes. The gate still selects the newest PR comment containing the `Code Review v2` heading through `gh pr view --json comments`. Target branch names may contain backticks, and the target SHA may advance. Each pass checks base freshness against the same PR snapshot used for the approval binding. Bureau authenticates the newest visible review in complete gate snapshots and atomically pins the head.

Bureau prepares the merge message before the final checks, unconditionally re-reads the approved target, checks ticket authorization immediately before merge, and checks merged state, merge commit and target afterward. A wrong target or an outcome that remains uncertain logs an error, posts a Linear comment and holds with `needs-human` and exit 25. An uncertain result gets one fresh reconciliation read; rollback and automatic merge retry are excluded from that response.

A stale approval returns to Build Review with exit 20 and a `refreshed` report even if earlier gate comments cannot be read. Shepherd uses `agents.max_review_rounds` as a separate stale-handoff limit per run, counted after the move to Build Review is confirmed. Human holds take effect immediately during confirmation; a move still invisible after one bounded polling budget returns quiet retry 2. Stale approvals preserve the dispatcher's base-refresh allowance.

The inline route reviews and cleans up against the PR's advertised head, including same-named fork branches, force-pushes and deleted or pruned branches. A failed cleanup fetch uses the pinned review base and returns quiet retry 2. The acceptance brief credits a verdict only to its separately recorded reviewed head, and Shepherd preserves post-merge reconciliation reports.

Target validation stays observational. GitHub has no expected-base merge option, so a retarget after the final read can still change the destination. The post-merge check detects that outcome; destination selection remains subject to that race.

### Narrower Trivy inline-ignore stripping and Yarn parsing

For repositories using Trivy QA, Bureau strips ignore comments only in explicitly recognised per-file shapes with fully classified prefixes. Every other marker line stays byte-identical and reduces coverage with `inline ignore format not recognised`. YAML node properties, block scalars and flow collections, HCL interpolation and heredocs, Bicep multi-line strings and other unrecognised constructs also preserve all later marker lines. Dockerfiles retain every marker from the first `<<` through end of file without parsing delimiters. Ordinary supported comments, string and data bytes, whitespace and LF/CRLF handling are preserved.

Yarn plain values now use an explicit character and leading-form allow-list for tool-written tokens, compact ranges, protocol references, URLs, hashes and checksums, booleans, cache keys, bin paths and platform conditions. Unsupported leading forms, `: ` and ` #` reduce coverage with `lockfile format not recognised`. Quoted values and recognised Yarn v1/Berry metadata keep their existing handling. Parser and neutralization identities change, so earlier receipts are remeasured once.

### v3.9.0 publication record

The source-only record captures the published tag and merge commit, GitHub Release, release review rounds, both gate suites, CI results passing on the first attempt without reruns and the tested-tree comparison. See the unchanged [v3.9.0 release notes](release-notes-v3.9.0.md).

## New files

| File | Installation and purpose |
|---|---|
| `templates/scripts/bureau-hold-context.py` | Complete scripts resync together with interfaces for implementation decision context. |
| `tests/hold_answers_test.py`, `tests/test_hold_answers.sh`, `tests/test_inline_merge_regressions.sh` | Repository-only regression entry points. |
| `docs/release-notes-v3.9.0.md` | Unchanged archive of the previous release notes. |

## Configuration and exit-code changes

Configuration keys, defaults, commands and the v3 exit vocabulary stay unchanged. Semgrep and Trivy retain their existing opt-in settings. The answer rule and approval binding apply when their respective runtime paths run.

| Behaviour | Action / result |
|---|---|
| Implementation decision answers | Reissue open holds with fresh IDs and obtain later replies beginning with the exact `Answer: <hold-id>` line and a nonblank body. Completed holds stay as they are. |
| Older approvals without binding lines | One fresh review is required. |
| Review evidence | Resync evidence writers and readers together. Older readers do not understand the new optional `reviewed_head` round field; legacy records still use matching receipts. |
| Trivy receipts | Earlier receipts are remeasured once because parser and neutralization identities change. |

A stale approval uses existing exit 20 and returns to Build Review. A wrong-target or still-uncertain merge outcome holds with exit 25. A definitive blocker such as a merge conflict also returns 25 when the base name is missing; a failed read with only undecided blockers returns 2. See [exit codes](exit-codes.md).

## Upgrade an existing installation

**From v3.9.0, once v3.9.2 is published.** Drain old drivers and stop every shepherd, queue loop, dispatcher and supervisor. Preserve unfinished work, local commits, customized assets, user instructions, Spec Kit assets and constitution, and shared runtime records. Compare installed drift and reconcile local incident fixes. Record the previous source commit and save the [pre-upgrade inventory and private backup](migration.md#before-changing-an-adopting-repository), including `.bureau-install.json` and `.gitignore`. Select the published `v3.9.2` tag and refresh discovery.

Use the [v3.9.2 preview and apply commands](migration.md#from-v390) in each adopting repository with its existing target selection. Resync the **complete scripts scope together with interfaces**, including evidence writers and readers. Resolve conflicts before applying. `--update` edits configuration only.

Before re-enabling dispatch, reissue every open implementation hold with a fresh ID and its still-open questions, including tickets whose label was already removed. Keep each ticket held and ask humans to repost earlier decisions as later replies beginning with the exact `Answer: <hold-id>` line and a nonblank body. Completed holds stay as they are. Obtain one fresh review for older approvals without the binding lines. Expect earlier Trivy receipts to be remeasured once.

Run `python3 scripts/bureau-doctor.py --mode background` for a background installation or `python3 scripts/bureau-doctor.py --mode app` for an app-only installation and reconcile holds. Restart all drivers together. If dispatch was paused for the upgrade, run `python3 scripts/bureau-runtime.py unpause` after restart and hold reissue. Qualify a representative ticket before leaving dispatch unattended. Live adopter qualification remains pending.

**From earlier versions.** Apply the cumulative steps through [v3.9.0](migration.md#upgrade-to-v39) first, then this upgrade. The v3.9.0 publication record itself needs only a source checkout update.

## Compatibility and rollback

Configuration schema versions 1 and 2, Bash 3.2+, Python 3.9+, Node 18+ for the shared scheduler, Spec Kit 1.2.2 and the current-workspace prepare/finish protocol apply. Scanner platform prerequisites remain as documented in the [v3.9.0 compatibility record](release-notes-v3.9.0.md#compatibility-and-rollback). Writers and readers of review evidence need a coordinated resync because of the optional `reviewed_head` round field; legacy records still use matching receipts.

For rollback, stop drivers and reconcile unfinished writers, local commits and holds. Follow the [restore procedure](migration.md#restore-the-pre-upgrade-installation) to restore the complete v3.9.0 asset set, compatible configuration and installer records from the private backup. Retain runtime and scanner records for inspection. Prior commits, pushes, comments, labels and state moves persist through rollback.

## Validation

The merged content baseline is main `5b0c279`. The merged changes follow first-parent merge order.

| PR | Merge commit | Change |
|---|---|---|
| [#165](https://github.com/KaiaK808/bureau/pull/165) | `b49eb97` | Narrower Trivy ignore-comment stripping and Yarn plain-value parsing |
| [#166](https://github.com/KaiaK808/bureau/pull/166) | `888f92a` | Repository-only shard-weight rows for two scanner test files |
| [#167](https://github.com/KaiaK808/bureau/pull/167) | `f1caef0` | v3.9.0 publication record |
| [#168](https://github.com/KaiaK808/bureau/pull/168) | `f82ef56` | Exact implementation hold answers and decision context through retries |
| [#169](https://github.com/KaiaK808/bureau/pull/169) | `5b0c279` | Merge approval binding, stale handoffs and reviewed-head attribution |

The fragments record local hold-answer fixtures with fake Linear and providers. Merge and review validation uses focused offline tests with fake GitHub responses and private reverted copies, including worker and Shepherd retries, final-read withdrawal and acceptance attribution. Live GitHub behaviour remains unverified offline.

Trivy validation records offline parser and pipeline regressions, fail-before checks against the previous revision, realistic Yarn v1/Berry locks including CRLF, Python compilation and changelog validation. Live scanner and adopter qualification are excluded. The v3.9.0 publication facts are maintainer-supplied. See the [fragment validation in the changelog](../CHANGELOG.md#392---2026-11-24).

The release branch's own validation remains to be run by the maintainer:

| Release check | Result / reference |
|---|---|
| Independent release review | TBD |
| macOS gate suite and tested ref | TBD |
| Remote gate suite and tested ref | TBD |
| Release CI jobs, runs and results | TBD |
| Final candidate/tag tested-tree comparison | TBD |
| Manual live injection sample and adopter qualification | TBD |

Before publication, record the exact tested refs and results and run [release tree verification](releases.md#verify-the-tested-release-trees). The manual live injection sample remains a separate release-checklist item.

## Known limitations

- Target validation is observational. A retarget after the final read can still change the merge destination; the post-merge check detects it. GitHub provides no expected-base merge option.
- Live GitHub behaviour, the Linear editor round-trip, live scanner behaviour and adopting-repository qualification remain unverified by the offline validation recorded in the fragments.
- Trivy preserves unrecognised inline-ignore shapes and reports reduced coverage. The conservative Dockerfile rule retains every marker from the first `<<` through end of file; unsupported Yarn plain values also reduce coverage.
- The dispatcher shutdown test can fail intermittently because the run's leases are marked interrupted a few milliseconds after the dispatcher exits. This path predates this release and remains unchanged in v3.9.2.
- The release branch's own macOS and remote suites, CI, tested refs and final tree comparisons remain `TBD` until the maintainer runs them.
