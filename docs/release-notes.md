# Bureau v3.10.0 — Merge charter, inherited-profile doctor and commit secret scan

**Release preparation · planned 2026-12-01 · source tag `v3.10.0` once published.** v3.10.0 is a minor release on [v3.9.2](release-notes-v3.9.2.md). Tagging and publication remain separate maintainer operations; this document does not announce a published release.

The release includes [#170](https://github.com/KaiaK808/bureau/pull/170), [#171](https://github.com/KaiaK808/bureau/pull/171), [#175](https://github.com/KaiaK808/bureau/pull/175), [#174](https://github.com/KaiaK808/bureau/pull/174), [#173](https://github.com/KaiaK808/bureau/pull/173), [#176](https://github.com/KaiaK808/bureau/pull/176), [#177](https://github.com/KaiaK808/bureau/pull/177), [#179](https://github.com/KaiaK808/bureau/pull/179), [#178](https://github.com/KaiaK808/bureau/pull/178), [#181](https://github.com/KaiaK808/bureau/pull/181), [#180](https://github.com/KaiaK808/bureau/pull/180), [#182](https://github.com/KaiaK808/bureau/pull/182) and [#183](https://github.com/KaiaK808/bureau/pull/183) in first-parent merge order. All content changes are merged. The [changelog](../CHANGELOG.md#3100---2026-12-01) collects all 12 release fragments; #182 changes only a test timing row and has no fragment.

## What changed since v3.9.2

### Merge charter: trusted policy, enrollment, queue and exceptions

The new opt-in `agents.merge_charter` declares a strictly validated merge policy. A maintainer publishes sanitized policy on the actual default branch and explicitly enrolls the repository with `bureau-charter.py enroll`. Enrollment lives in the shared Git directory, so linked worktrees share it and independent clones do not. Installation and resync never enroll. Both merge gate passes use a pinned default-branch policy, fetched with hooks and submodule recursion disabled; PR-controlled configuration cannot disable enrollment. Enrolled policy errors, indeterminate enrollment and PR changes to `.bureau.json` require a human with exit 25. Doctor checks declarations, enrollment, locally fetched policy and digest drift without creating state or making remote reads.

For unenrolled repositories, a merge run adds exactly one local `git rev-parse --is-bare-repository --show-toplevel --git-common-dir` read from the working directory, with hooks disabled and inherited `GIT_*` overrides removed for that call only. Its unexported result is reused through `status --common-dir` at the initial probe and both gate passes. It adds no GitHub CLI or network calls. A failed lookup, bare repository or missing work tree blocks with `charter-policy` and 25; run the merge stage from a work tree and repair inaccessible enrollment before retrying.

Enrolled policy can require an approved Linear label or cycle and reserve exception classes by committed path or exact ticket label. A missing approval waits with 2; exceptions and indeterminate classification take precedence with 25. Classification covers deleted paths and both rename/copy endpoints, and requires complete non-shallow ancestry. Queue approval and exception labels share a complete ticket snapshot. Both merge routes compare the final live target tip with the classified target SHA, even with scanners or freshness checks off; movement retries with 2. This read precedes the ticket kill-switch checkpoint and the head-pinned merge. GitHub's merge operation has no expected-base option, so target observation and merge remain separate operations.

Doctor reports queue mode and exception classes. `suite`, `red_main` and `report` are validated declarations only: suite receipts, merge reports, main-CI latches and concurrency enforcement are outside this release. The queue and exception gates extend the earlier foundation fragment; they are active for enrolled repositories in the final combined release. See [merge charter configuration](configuration.md#merge-charter-foundation).

### Inherited Claude profile checks and auto-memory off

Every background Claude child gets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` after environment filtering, even if the parent sets it to 0. The guard includes stage calls, review helpers, conflict and upstream helpers, login checks, CLI capability probes and Headroom launches. Interactive Claude outside Bureau is unchanged.

Doctor warns once about stages inheriting the operator profile and lists only user MCP server names and enabled-plugin names, with their source-file paths. Missing files mean empty lists; unreadable or invalid files produce explicit `could not check` warnings and unknown lists. Project/managed settings and plugin-provided servers are outside this inventory. Doctor remains read-only and does not persist a drift baseline. A lean `agents.providers.claude.config_dir` or overriding `BUREAU_CLAUDE_CONFIG_DIR` silences the inheritance warning and needs its own login. See [Claude configuration directory](provider-runtime.md#claude-configuration-directory).

### Scan what the agent commits as a review floor

Code review always scans added committed lines between the pinned merge base and head for known credential formats and long high-entropy secret assignments. It reads Git objects without repository-controlled commands, external diff drivers, providers, scanner binaries or databases. There is no enable switch or Trivy prerequisite. Open findings and unavailable or malformed measurements prevent approval, including approval reuse; BLOCK survives and review caps still apply.

Findings mask detected values, paths and rule IDs after the complete scan, including later, suppressed and omitted detections. Scanner, redaction or locator exceptions discard partial output and return a blocking not-measured result without raw diagnostics. Each finding carries a pinned head blob ID and a 16-hex path fingerprint for resolving redacted paths; incompatible parser formats remain not measured. Proven deletions skip path budgets and blob reads. Narrow exceptions come only from the pinned base's `.bureau/secret-exceptions.json` and need an owner, reason and future expiry. This check does not scan intermediate history, decoded content, unchanged lines or commit messages; masking does not cover undetected values or arbitrary transformations. See [committed secrets](configuration.md#committed-secrets).

### Acceptance contracts without enforcement

Optional version 1 declarations map Spec Kit requirement and task references to intended checks or human-reserved decisions. A standard-library offline CLI validates the closed schema, bounds and references; synthetic examples cover both forms. No stage, gate or prompt reads the contract and there is no enablement key. A well formed declaration proves neither completeness nor verifier existence, success or product acceptance. See the [format and runnable examples](../references/acceptance-contracts.md).

### Trusted merge publishers

Automatic merges now count only the newest visible review from a typed publisher in default-branch `agents.merge_publishers`, using complete snapshots, strict headers and same-node edit checks for every verdict. Missing or invalid policy holds automatic merging with 25. Doctor warns on absent policy in manual mode and rejects present invalid values in either mode. Publication failure stops review explicitly with 18; edited reviews need fresh Build Review with 20, and incomplete snapshots retry with 2. A dedicated Bureau App is preferred: a shared human or Actions principal trusts everyone acting as that identity. Deletion of a newer verdict without trace remains undetectable. Head and target safeguards remain in force. See [trusted merge publishers](migration.md#trusted-merge-publishers).

### Dormant shared test wrapper

`bureau-test-wrapper.py` supplies trusted literal argv policy, private child-start/end proof, a queue-aware budget clock, cancellation teardown and read-only doctor diagnostics. `repo.test_wrapper` is absent or empty by default, and `repo.test_wrapper_start_timeout_seconds` defaults to 21600 within (0, 86400]. The primitive rejects checkout-owned symlink traversal and distinguishes unavailable authority from invalid policy. Existing stage routing is not connected: configuring the wrapper changes no current launch, timer, build reuse key or evidence format. See [test wrapper configuration](configuration.md).

### Local suite serial reruns and timing data

The repository-only `tests/run_suite.py` waits for every shard before rerunning eligible failed files once, serially, within a five-file/3600-expected-second budget. Deadlines stay unchanged. Results are atomic, and a locked file-level flake ledger feeds a proposal outbox. A recovered green means an observed file-level flake, not a proven cause. Hosted CI and direct `tests/run.sh` calls never retry. External wrappers must keep exclusive suite/worktree admission, use a clean checkout, keep output and stable state outside the worktree, validate receipts and consume proposals. Detached processes outside the test group and persistent fixtures are not tracked. #182 records the measured charter queue driver duration for sharding. See [local full-suite operation](../tests/README.md#local-full-suite-and-serial-reruns).

### Dispatcher shutdown lease settlement and process-control limits

After joining direct lane children, dispatcher cancellation observes its launched runs during one shared five-second settlement wait. It ends with settled ownership or a best-effort `human_hold` diagnostic naming each unresolved run and issue. Uncertain leases remain retained; unrelated groups are never signalled. Diagnostic write/flush failures preserve cancellation exit 130, including at interpreter shutdown. Existing nested shutdown grace periods remain unchanged.

Process-control regressions use bounded 30-second startup, exit and recovery waits on loaded runners, and clean up the recorded child group before removing temporary files. Production inspection remains fail-closed with its two-second timeout; diagnostics add a short cause without raw process output. `BUREAU_TEST_DISPATCH_SETTLE_SECONDS` and `BUREAU_TEST_PROCESS_INSPECT_SECONDS` are bounded test-only overrides, not operator tuning keys. No configuration or store migration is required.

### Security reporting and the v3.9.2 publication record

The security policy supports the latest published tag, lands fixes on main for the next tagged release and directs reports to GitHub private vulnerability reporting. Branch examples use generic issue keys. These documentation changes need no adopter action.

The v3.9.2 publication record preserves its tag, merge commit, release review rounds, first-pass gate suites, CI history including cancelled main runs and the passing tag run, and tested-tree comparison. It needs only a source checkout update. See the unchanged [archived v3.9.2 notes](release-notes-v3.9.2.md) and [validation record](releases.md#v392-validation-record); these are historical facts, not v3.10.0 validation.

## New files

| File | Installation and purpose |
|---|---|
| `templates/scripts/bureau-charter.py` | Scripts scope; trusted policy, explicit enrollment and merge gates. |
| `templates/scripts/bureau-merge-review.py` | Scripts scope; typed publisher and review snapshot validation. |
| `templates/scripts/bureau-secrets.py` | Scripts scope; committed-secret review floor, updated together with its parser. |
| `templates/scripts/bureau-test-wrapper.py` | Scripts scope; dormant test execution primitive. |
| `templates/scripts/bureau-acceptance-contract.py` | Scripts scope; optional standalone declaration validator. |
| `references/acceptance-contracts.md`, `references/acceptance-contract-examples/` | Source-only format guide and synthetic examples. |
| `tests/run_suite.py`, `tests/run_suite_test.py`, related regression files | Repository-only suite orchestration and coverage. |
| `docs/release-notes-v3.9.2.md` | Unchanged archive of the previous release notes. |

## Configuration and exit-code changes

| Setting / behavior | Default and operator action |
|---|---|
| `agents.merge_charter` | Absent/off. `enabled`, `approved_queue`, `exceptions`, `suite`, `red_main`, `report` are strict policy fields. Publish sanitized default-branch policy and explicitly enroll to activate gates. Existing enrollment stays active. |
| `agents.merge_publishers` | Required valid typed allowlist for automatic merges; publish before resync. Absent policy in manual mode warns; invalid present policy errors. |
| `agents.providers.claude.config_dir`, `BUREAU_CLAUDE_CONFIG_DIR` | Existing optional lean-profile controls; environment wins. Separate login required. |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | Forced to 1 in background Claude children, including when the parent supplies 0. |
| Committed-secret floor | Always active when review runs; no setup or enable switch. Base-only exceptions require review. |
| `repo.test_wrapper` | Absent or `[]`, disabled and dormant; a literal argv array cannot activate stage routing. |
| `repo.test_wrapper_start_timeout_seconds` | 21600; finite number greater than 0 and at most 86400, validated even while disabled. |
| Acceptance contracts | Optional offline declarations; no configuration or enforcement key. |

The configuration schema versions and v3 exit vocabulary remain. New charter paths use existing 2 for waits/retries and 25 for human handoff. Semgrep, Trivy and existing advisory checks retain their opt-in settings. No new runtime dependency is required: Bash 3.2+, Python 3.9+, git and jq remain, with Node 18+ for the shared scheduler and authenticated `gh` for background GitHub stages. The local suite uses Python standard-library locking on the existing macOS/Linux test platforms and the [existing harness prerequisites](../tests/README.md#leaf-runner-details).

## Upgrade an existing installation

**From v3.9.2, once v3.10.0 is published.** Follow [Upgrade from v3.9.2](migration.md#upgrade-from-v392) for runnable source selection, drain/backup, default-branch publisher policy, complete scripts preview/apply, optional profile setup, optional charter enrollment, history repair, diagnostics and restart/dispatch recipes. Compare installed drift before resync; install the charter/environment helpers and secret helper/review parser together. No interfaces, CI or workflows resync is required by this release. `--update` remains configuration-only.

Charter enrollment stays opt-in and requires a maintainer command; an enabled declaration alone is insufficient. Unenrolled merge runs still perform the one local Git read. The committed-secret floor and auto-memory guard apply without opt-in once the new scripts run. The test wrapper remains dormant. The offline acceptance tool is optional; the security/reporting and prior publication documentation need only the source update. Local suite wrapper adoption is a separate maintainer operation, with no hosted-CI or installed-runtime change.

**From earlier versions.** Apply the cumulative steps through [v3.9.2](migration.md#upgrade-to-v392) first, then this upgrade. Live adopter qualification remains pending.

## Compatibility and rollback

Configuration schema versions 1 and 2, Spec Kit 1.2.2 and the current-workspace prepare/finish protocol remain supported. The existing scanner platform constraints remain in the [v3.9.0 compatibility record](release-notes-v3.9.0.md#compatibility-and-rollback). The secret scan adds no external scanner requirement. Enrolled classification requires full history; corrupt nested bare repositories without HEAD remain unsupported worktree inputs. Target observation is not atomic with merge.

For rollback, stop all drivers and reconcile unfinished writers, local commits and holds. Follow the [restore procedure](migration.md#restore-the-pre-upgrade-installation) to restore the complete v3.9.2 asset set, compatible configuration and installer records from the private backup. Explicitly unenroll with the trusted new helper before reverting an enrolled installation to a runtime without charter enforcement. Retain shared runtime, scan and enrollment evidence for inspection; do not delete leases to clear a hold. Rollback does not undo commits, pushes, comments, labels or state moves.

## Validation

The merged content baseline is main `6c806cc`. Changes follow first-parent merge order:

| PR | Merge commit | Change |
|---|---|---|
| [#170](https://github.com/KaiaK808/bureau/pull/170) | `a7c66d8` | Local suite serial file reruns and flake records |
| [#171](https://github.com/KaiaK808/bureau/pull/171) | `16af1d5` | Dormant test wrapper primitive |
| [#175](https://github.com/KaiaK808/bureau/pull/175) | `60ffef8` | Trusted review publishers for merge |
| [#174](https://github.com/KaiaK808/bureau/pull/174) | `584307d` | Acceptance-contract declarations and offline validator |
| [#173](https://github.com/KaiaK808/bureau/pull/173) | `ba8894b` | v3.9.2 publication record |
| [#176](https://github.com/KaiaK808/bureau/pull/176) | `2036b45` | Dispatcher shutdown lease settlement |
| [#177](https://github.com/KaiaK808/bureau/pull/177) | `ffed1ec` | Security policy and generic branch examples |
| [#179](https://github.com/KaiaK808/bureau/pull/179) | `35ceafb` | Committed-secret review floor |
| [#178](https://github.com/KaiaK808/bureau/pull/178) | `b250952` | Merge charter foundation, enrollment and doctor |
| [#181](https://github.com/KaiaK808/bureau/pull/181) | `b0137d9` | Enrolled approved queue and exception classes |
| [#180](https://github.com/KaiaK808/bureau/pull/180) | `f50a420` | Inherited Claude profile doctor and memory guard |
| [#182](https://github.com/KaiaK808/bureau/pull/182) | `aa4964d` | Charter queue test timing row; no fragment |
| [#183](https://github.com/KaiaK808/bureau/pull/183) | `6c806cc` | Process-control test waits and inspection diagnostics |

The fragments record focused offline parser, schema, mocked-object, fake-provider and private-fixture checks at their individual revisions. Publisher changes include adversarial and mutation controls. Dispatcher settlement has maintainer-run isolated shutdown cases and an observed failure against the previous runtime. Process-control changes record two individual runtime tests, one requiring unrestricted process inspection, plus a scratch-only negative control; added bounds and failure regressions were not run under that change's execution limit. Security reporting records its documentation check under the shared slot.

Those records do not qualify this combined release. Charter real-Git layouts and merge-route fixtures, secret real-Git and shell/pipeline regressions, wrapper platform/live-slot integration, real operator profiles and provider behavior, host-load behavior and full suites remain maintainer checks as detailed in the [fragment validation](../CHANGELOG.md#3100---2026-12-01). Historical fragment statements about checks not run describe those change revisions. No release suite, live provider or adopting-installation validation is claimed here.

The release branch's own validation remains to be run by the maintainer:

| Release check | Result / reference |
|---|---|
| Independent release review rounds | TBD |
| macOS gate suite and tested ref | TBD |
| Remote gate suite and tested ref | TBD |
| Release PR CI jobs, runs and results | TBD |
| Release-branch and main push CI jobs, runs and results | TBD |
| Tag CI jobs, run and result | TBD |
| Final candidate/tag tested-tree comparison (`release_verify`) | TBD |
| Tag object and merge commit | TBD |
| Publication date | TBD |
| Manual live injection sample and adopter qualification | TBD |

Before publication, record exact refs and results and run [release tree verification](releases.md#verify-the-tested-release-trees). The manual live injection sample remains a separate checklist item. This release preparation changes documentation only; templates, tests and scripts must remain identical to `6c806cc`.

## Known limitations

- Charter gates are a local accident guard. Manual merges, independent clones and deliberate bypass with the same user permissions are outside it; final target observation and merge are separate operations.
- Charter suite receipts, reports, red-main latches and concurrency enforcement are not implemented. Acceptance contracts validate declarations only, and test wrapper stage routing is dormant.
- Shared publisher identities trust everyone acting as that principal, and deletion of a newer verdict without trace is undetectable.
- The committed-secret scan covers literal added physical lines within its bounds, not complete history or decoded/split secrets; unmeasured scans block approval. Masking is limited to detected values and specified rule-masked forms.
- Doctor inventories user-profile names only and persists no drift baseline. Live profiles/providers, services, scanner behavior and adopting installations remain unqualified by these offline records.
- Local serial recovery is an observed file-level flake, not a cause diagnosis; external admission and receipt validation remain required.
