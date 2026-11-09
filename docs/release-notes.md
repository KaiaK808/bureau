# Bureau v3.7.0 — preserved work, design guidance and release tooling

**Published release · 2026-11-09 · source tag `v3.7.0`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.7.0) is published as latest. Annotated tag `v3.7.0` (tag object `02406d8`) points at merge commit `4366c91` of [#135](https://github.com/KaiaK808/bureau/pull/135), merged on 2026-11-09. v3.7.0 is a minor release on [v3.6.0](release-notes-v3.6.0.md).

The release includes, in first-parent merge order: [#114](https://github.com/KaiaK808/bureau/pull/114), [#115](https://github.com/KaiaK808/bureau/pull/115), [#116](https://github.com/KaiaK808/bureau/pull/116), [#119](https://github.com/KaiaK808/bureau/pull/119), [#117](https://github.com/KaiaK808/bureau/pull/117), [#118](https://github.com/KaiaK808/bureau/pull/118), [#121](https://github.com/KaiaK808/bureau/pull/121), [#120](https://github.com/KaiaK808/bureau/pull/120), [#122](https://github.com/KaiaK808/bureau/pull/122), [#123](https://github.com/KaiaK808/bureau/pull/123), [#124](https://github.com/KaiaK808/bureau/pull/124), [#125](https://github.com/KaiaK808/bureau/pull/125), [#126](https://github.com/KaiaK808/bureau/pull/126), [#127](https://github.com/KaiaK808/bureau/pull/127), [#128](https://github.com/KaiaK808/bureau/pull/128), [#129](https://github.com/KaiaK808/bureau/pull/129), [#131](https://github.com/KaiaK808/bureau/pull/131), [#130](https://github.com/KaiaK808/bureau/pull/130), [#132](https://github.com/KaiaK808/bureau/pull/132). This includes the v3.6.0 publication record in #114 and the accepted control-plane design in #115; neither adds a new runtime. The [changelog](../CHANGELOG.md#370---2026-11-09) is the consolidated change and upgrade record.

## What changed since v3.6.0

### Preserve local work before stage starts

Ticket-stage checkout and disposable-worker reset hold with **30 / `local-commits-held`** before dropping commits reachable only from the target branch or its attached/detached HEAD. Other local branches and all remote refs count as retained reachability. The hold names each commit and leaves the branch and checkout intact. Push work that must survive; an explicit one-run `BUREAU_DROP_LOCAL_COMMITS=1` allows discarding it and prints/logs full discarded SHAs.

Reset-time holds preserve the worker through dispatcher reconciliation; the tick reports `blocked` with exit 30. Recovering a clean held worker with the override still requires ownership and a preservation record matching the issue, target branch and worktree identity, including when only the branch ref retains unpublished commits. Older hold records without that identity require explicit recovery. Recorded Bureau review checkpoints and successful worker-stage heads keep their existing resume/stall behavior only when exact heads and worktree identities match; author or message resemblance does not exempt operator commits. See [exit-30 recovery](troubleshooting.md#exit-30-local-commits-held--unpublished-commits-before-a-reset).

Shepherd starts at QA, Build Review, Merge or Done require the ticket branch on origin and an open PR before any claim, label, Linear move or stage work. A confirmed missing prerequisite holds with **31 / `open-pr-required`** and prints a PR creation command with the configured base plus the original restart command. Push/open the PR, then rerun that command. Lookup errors remain 18; direct review retains 15 for a confirmed missing PR. Build and earlier starts are unchanged. Review and shepherd share implement's bounded transient PR lookup retries. See [exit-31 recovery](troubleshooting.md#exit-31-open-pr-required--restart-after-a-build-gate-failure).

### Optional local Impeccable guidance and evidence

Per-repository `design.impeccable` is off by default. When enabled with verified local tooling, UX references the installed playbooks and preserves project rules in root `PRODUCT.md`/`DESIGN.md`; implementation feeds offline mechanical/deferred detector findings into its bounded loop. Review saves independent specialist judgements first, then considers detector findings as late machine evidence with separate receipts. Late evidence cannot erase the independent findings or holds.

Bureau verifies skill **4.8.0** and engine **0.1.18** without downloading or installing Impeccable, its hooks or agents. Operators supply the pinned tooling separately with hooks disabled and remove/disable any separately installed agents. Local snapshots, disabled telemetry, exact reasoned ignores and resolved in-repository paths constrain measurement. Unavailable tooling is explicit; failed measurement and outside-repository findings never mean clean. Cancellation latches SIGINT/SIGTERM across launch until the engine can be killed and joined, then removes snapshots and restores handlers. Disabled integration preserves existing prompts and checks. See [configuration](configuration.md#designimpeccable) for the complete contract.

### Provider, PR and admission reliability

Implementation retries transient post-push PR lookup, ready, view and create failures on both ready and draft paths. Defaults are three retries after 2/4/8 seconds; `BUREAU_IMPL_PR_RETRIES` and `BUREAU_IMPL_PR_RETRY_WAIT` provide bounded overrides. Failed lookup never implies no PR. Exhaustion holds with 18, identifies the pushed branch and asks the operator to rerun the stage before any hand-off.

Provider classification reads terminal Codex/Claude errors and CLI stderr, excluding tool/agent text and MCP server authentication notices. Capacity/overload retains exit 23 and genuine CLI authentication failure retains 16. Retry policy and evidence schema remain unchanged.

Read-only Codex runs keep the repository unwritable and gain a private writable scratch directory even with caller-supplied `TMPDIR`. Explicit CLI sandbox overrides prevent inherited writable roots/network settings from widening access; the prompt supplies a shell-quoted repository path for reads. Scratch cleanup covers completion, failure and stops. Optional per-run sandbox/command metadata needs no evidence schema migration; workspace-write and Claude behavior remain.

File-overlap admission scopes Linear comments/snapshots to `linear.projects` while retaining candidates and issues with reservations, waiters, migration entries or leases. Title/description edits no longer invalidate snapshots; membership, state and label edits still do. Shepherd uses the dispatcher's three-attempt bound. Provenance and ownership are rechecked against the final unfiltered snapshot; uncertain cross-project PRs retain their diffs. State/reservation formats and exits remain unchanged.

Shepherd `--from-stage` can move inactive/backlog tickets directly to ungated targets, respecting configured active states and strict withdrawal checks after the move. Gated targets refuse with 25 before claiming or moving; operators first move the ticket into an active state. Linear read failures retain 10, and existing ownership, human-hold and budget/progress rules remain.

### Cheaper startup and read-only baseline reporting

Pipeline startup uses one framed source-time `jq` read instead of 35 while preserving defaults, warnings and live lookups. Unused settings and the unused QA sandbox predicate are removed. `repo.commit_prefix` never affected behavior and can be deleted. Doctor caches helper imports, crosschecks resolve their prefix once, and the legacy dashboard reads only its last 200 log lines. Review merging/evidence reconstruction reuse imports per invocation without changing output, ordering, digests or exits. The CI-marker check batches commit-object reads with length and range checks, fails closed on read/scratch errors, and cleans up on INT/TERM/HUP.

The source-only command `python3 -I scripts/bureau-baseline.py --repo PATH [--weeks N] [--format md|json]` reports retained weekly completions, abandonment, review rounds, costs, attribution and defect back-references with sources and missing measurements. Eligibility history and Triage entry stay unavailable rather than inferred. It needs local Git, authenticated `gh` and optional jq/Linear credentials; it installs no runtime assets. See [the maintainer guide](../CONTRIBUTING.md#weekly-baseline).

### Contributor tests, CI and release checks

Pull requests run all Linux tests in four shards and the focused 56-entry macOS manifest in two. Main, release-branch and tag pushes, weekly schedules and manual dispatch retain all six full macOS shards. `BUREAU_TEST_MANIFEST` filters before sharding and rejects invalid/empty selections. Platform-sensitive additions need a manifest entry and reason; focused PR CI is not full macOS coverage. See [contributor coverage](../CONTRIBUTING.md#ci-coverage).

Per-file process groups have weighted deadlines of at least ten minutes, TERM/KILL cleanup and explicit timeout results; `BUREAU_TEST_FILE_TIMEOUT` overrides them. Optional `BUREAU_TEST_TIMINGS` records duration/outcome/OS, and `tests/update_shard_weights.py` accepts successful records for the matching OS. Focused mutation controls require declared detection while ordinary positive runs keep complete matrices. Fixture hygiene adds injected clocks/process tables, readiness signals, batched PID audits, bounded inspection retries, disabled automatic Git maintenance and one-call runtime copies. The dispatcher shutdown fixture observes wrapper exit through a FIFO that closes under SIGKILL; the cancellation probe targets the populated check's isolated process group.

The source-only `scripts/release_verify.py` compares two refs and rejects changes in `templates/`, `tests/` and `scripts/`, excluding only itself and `tests/test_release_verify.sh`. It prints differing paths and permits documentation-only changes. Use it between the release branch head that ran the full macOS suite and the candidate/tagged commit; see [release verification](releases.md#verify-the-tested-release-trees). It checks tree identity, not test results.

### Documentation and accepted design

The README now starts from the Linear workflow, human merge/hold boundaries, isolation and requirements; the static manual gains a light/dark SVG. [ADR 0014](adr/0014-control-plane.md) is `accepted (design)`: one owning authority, transactional state/outbox and a supervised review-to-routing pilot. It has no implementation, cutover or upgrade action. The v3.6.0 notes are [archived unchanged](release-notes-v3.6.0.md).

## New files

| File | Installation and purpose |
|---|---|
| `templates/scripts/bureau-impeccable.py`, `bureau-impeccable.sh`, `bureau-impeccable-pins.json` | Installed by the complete scripts resync; local verification, detector integration and pinned tooling digests. |
| `scripts/bureau-baseline.py` | Source-only read-only maintainer report. |
| `scripts/release_verify.py`, `tests/test_release_verify.sh` | Source-only release tree comparison and fixture test. |
| `tests/macos-manifest.txt`, `tests/run_test_files.py`, `tests/update_shard_weights.py`, fixture helpers | Repository-only CI selection, deadlines, timing updates and fixture hygiene. |
| `docs/adr/0014-control-plane.md`, `docs/release-notes-v3.6.0.md` | Accepted design and previous-release archive. |

## Configuration and exit-code changes

No configuration schema migration or Spec Kit pin change is required. Optional settings stay off until selected. See [configuration](configuration.md#designimpeccable) and [exit codes](exit-codes.md).

| Key | Default / action | Effect |
|---|---|---|
| `repo.commit_prefix` | Delete if present; no replacement. | Withdrawn unused setting. |
| `design.impeccable.enabled` | `false` | Enables local guidance/detection only when explicitly selected. |
| `design.impeccable.skill_version` | `"4.8.0"` | Supported skill pin; local content must verify. |
| `design.impeccable.engine_version` | `"0.1.18"` | Supported engine pin and platform digest. |
| `design.impeccable.skill_path` | Absent | Optional directory; otherwise check `.claude/skills/impeccable`, then `.agents/skills/impeccable`. Relative paths resolve from `.bureau.json`. |

| Exit | Class | Operator action |
|---|---|---|
| `30` | `local-commits-held` | Inspect named commits and push them, or deliberately discard once with `BUREAU_DROP_LOCAL_COMMITS=1`; reconcile held-worker identity/ownership before recovery. |
| `31` | `open-pr-required` | Push the ticket branch to origin, open its PR using the printed command and restart with the exact printed command. |

Both exits are human holds; update custom wrappers/classifiers together with runtime assets. Keep the one-run discard override out of persistent settings. Direct review still uses 15 for confirmed no PR; lookup failures use 18.

## Upgrade an existing installation

**From v3.6.0:** stop every shepherd, queue loop, dispatcher and supervisor. Preserve unfinished work/local commits, custom scripts/interfaces, instructions, Spec Kit assets/constitution and shared runtime records. Compare installed drift and reconcile incident fixes before replacing assets. Select tag `v3.7.0`, refresh discovery, preview and resolve conflicts, then resync the **complete scripts and interfaces scopes together** in every adopting repository:

```text
/bureau-init --resync-interfaces --resync-scripts --target both
```

Use `$bureau-init` in Codex and retain the existing target selection. Include Impeccable helpers/pins even when disabled. `--update` changes configuration only; partial helper/pipeline updates are incompatible and running shells keep loaded code. Teach custom consumers exits 30/31, remove `repo.commit_prefix` if desired, and leave Impeccable off until separately provisioned and deliberately enabled. No state/evidence migration is required; older exit-30 records lacking identity still need explicit recovery. Run doctor, reconcile holds, restart drivers together and qualify a representative ticket before unattended use. Follow [Upgrade to v3.7](migration.md#upgrade-to-v37).

**From v3.5.1:** apply [Upgrade to v3.6](migration.md#upgrade-to-v36) and its [archived notes](release-notes-v3.6.0.md#upgrade-an-existing-installation) before these steps. Carry forward review/QA pins and schemas, Spec Kit helper-mode repair, offline dependency caches, accounting/witness and process-hold preservation. From earlier versions follow the migration guide's cumulative sections first.

**Contributors:** update the source checkout for the baseline report, focused macOS manifest, deadline/timing tools and release verifier; these source-only tools need no adopting-repository resync. Add platform-sensitive tests to the manifest with a reason.

## Compatibility and rollback

Configuration versions 1 and 2, Bash 3.2+, Python 3.9+, the Node 18+ shared scheduler and Spec Kit 1.2.2 remain. Exits 30/31 require coordinated consumers. Impeccable is optional local tooling; Bureau does not install it. See the [v3.6.0 archive](release-notes-v3.6.0.md#compatibility-and-rollback) for the inherited configuration, isolation and accounting contracts.

For rollback, stop all drivers and reconcile unfinished writers, local commits and holds; select `v3.6.0` and restore scripts/interfaces together or the private backup. Disable/remove the optional Impeccable settings and restore compatible customizations/consumers. Retain evidence and preservation records for inspection; an older runtime cannot enforce the new holds. Rollback cannot undo commits, pushes, comments, labels or state moves.

## Validation

The maintainer's validation record states that every v3.7 pull request was built by Codex in its own worktree, independently reviewed read-only by Codex (at most three rounds unless the maintainer approved an exception), and had every touched test run outside the sandbox. The full local suite ran in two shards on macOS, followed by CI on the pull request; each was merged with a merge commit. Earlier waves (#114–#126) were each Codex-reviewed, suite-tested and CI-green before merge; their individual records remain in the pull request descriptions.

The third-wave records below follow merge order:

| PR | Independent review | Local full suite | CI and additional evidence |
|---|---|---|---|
| [#127](https://github.com/KaiaK808/bureau/pull/127), focused macOS manifest | Approved in round 2. | 174/177 on the first pass under heavy host load; 177/177 after serial reruns of the three red files. | Green on the first run. PR macOS cost measured at 79 runner-minutes across two jobs (2332 s and 2399 s), down from about 290; the full macOS run on main after the merge was green. |
| [#128](https://github.com/KaiaK808/bureau/pull/128), small cleanups | Approved in round 4; the maintainer-approved fourth round was limited to the squash-marker cancellation probe. | 171/175 on the first pass under load; 175/179 after rebase onto main, with every red file green on serial rerun. | One Ubuntu shard rerun after the hosted runner lost communication with the server after 77 minutes in the test step; no test failure was recorded. Green after the rerun. |
| [#129](https://github.com/KaiaK808/bureau/pull/129), fixture hygiene | Approved in round 3. | 174/178 on the first pass; every red file green on serial rerun. | Green on the first run. |
| [#131](https://github.com/KaiaK808/bureau/pull/131), opt-in Impeccable | Approved in round 4; the maintainer-approved fourth round was limited to the SIGINT launch guard. | 174/178 on the first pass; red files green on serial or direct rerun. | Green on the first run. The detector test ran with a pinned real engine twice (20/20 cases, no skips) and without the engine. |
| [#130](https://github.com/KaiaK808/bureau/pull/130), local-commits hold | Approved in round 5; rounds 4 and 5 were maintainer-approved exceptions. The fifth addressed the full suite's regression where Bureau-made local commits were held with 30, fixed by trusting heads Bureau's records prove it made. | 175/180 on the first pass; red files green on serial or direct rerun. | The first run failed only on lint shards (ShellCheck SC2100, an unquoted assignment); quoted, then green. |
| [#132](https://github.com/KaiaK808/bureau/pull/132), post-Build PR prerequisite | Approved in round 1. | 173/179 on the first pass while stacked on #130; red files green on serial or direct rerun; touched tests green after the final rebase. | Green on the first run. |

The release branch itself: Codex release review approved in round 2 (round 1 found that an inherited `GIT_LITERAL_PATHSPECS` setting let the verifier pass differing trees; fixed with a regression case). The full local suite on macOS in two shards at `e31537e` passed 183 of 184 on the first pass; `test_squash_marker.sh` (the probe race above) passed on direct rerun. CI on [#135](https://github.com/KaiaK808/bureau/pull/135) at `a538849` passed all 16 jobs, including the full macOS suite. See the [release record](releases.md#validation-record) for the push-run evidence-lock retries and follow-up. `python3 scripts/release_verify.py e31537e v3.7.0` passed, comparing that locally tested release branch head with the published tag.

## Known limitations

- Several tests are load-sensitive and can time out when the host runs other suites at the same time, including dispatcher, review-convergence and deadline cases. They pass on serial rerun; making them load-independent is recorded for the next release.
- The squash-marker cancellation probe can race under heavy load; it passes on rerun.
- The provider adapter's SIGHUP cases and the shepherd worker SIGPIPE test fail when the suite starts with SIGHUP ignored, for example under `nohup`. Run it from a normal shell or background job.
- Impeccable pilots in adopting repositories have not run yet. The integration is opt-in and off by default; **not measured never counts as clean**.
