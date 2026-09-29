# Bureau v3.0.1 — a merge stage that says why it waits, and no second paid review

**Stable release · 2026-09-29 · source tag `v3.0.1`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.0.1) records the exact tagged commit. v3.0.1 is a patch release on [v3.0.0](release-notes-v3.0.0.md), superseded by the patch release [v3.0.2](release-notes.md); it changes the runtime in two places that the second live acceptance of v3.0.0 showed in the maintainer's pilot installation. The candidates were [v3.0.0-rc.2](release-notes-v3.0.0-rc.2.md) and [v3.0.0-rc.1](release-notes-v3.0.0-rc.1.md); the previous major release is [v2.0.0](release-notes-v2.0.0.md).

It keeps v3's exit-code contract and adds two codes for the merge stage: it now ends with `2` when its gate is not yet decided and with `25` when the gate is decided against the merge, where it used to end with `0` whenever it did not merge. Anything that reads the merge stage's exit code must accept both.

## What changed

- **The merge stage says why it did not merge** ([#27](https://github.com/KaiaK808/bureau/pull/27)).
  - `2` (not yet): checks pending or not started, GitHub still computing the merge state, a gate read that failed (the pull request's state, its review comments or its review threads), a hold label `wip`, `blocked` or `needs-human` on the pull request, and conflicts the rebase stage resolves in the queue (`agents.rebase` on and a bureau-only divergence).
  - `25` (blocked): a failing check, other conflicts, a stale base, no APPROVE, unresolved review threads, or a pull request that is not open. An unreadable thread list used to count as zero unresolved threads.
  - The queue loop logs `2` quietly and alerts on `25`, at most once an hour per ticket; `bureau-tick.sh --allow-merge` reports them as `waiting` and `blocked`. The review stage's inline merge and `merge-pipeline.sh --dry-run` keep ending with `0`. With `BUREAU_MERGE_GATE_REPORT` set, the stage writes the outcome and the gate lines to that file.
  - The shepherd waits on a gate that is not yet decided (`BUREAU_SHEPHERD_MERGE_POLL_SECONDS`, default 60; at most `BUREAU_SHEPHERD_MERGE_WAIT_SECONDS`, default 1800; read in base 10 and capped at 1 h and 6 h) without the confirmation re-reads and without counting as stuck, and halts on a blocked gate, or on a wait that ran out, with `25`, `needs-human`, a comment carrying the gate report and an alert. While the shepherd holds a ticket, conflicts are blocked at once, because the queue's rebase stage skips tickets the shepherd holds.
  - The gate comment on the pull request states the outcome and is posted again only when the outcome or a blocker changes. Nothing merges on a red or pending gate; the just-in-time recheck before `gh pr merge` applies the same outcomes.
- **A resumed review reuses an unchanged approval** ([#26](https://github.com/KaiaK808/bureau/pull/26)). A review that stopped before merge (`--no-merge`, `BUREAU_STOP_REQUESTED`) records its verdict with its inputs. When the review stage runs again without a stop and the verdict, branch, ticket state, head commit, base commit, base branch, pull request and ticket fingerprint (title, description, labels) all match, it reuses that APPROVE without a model call; the build check still runs, and a red build folds the reused APPROVE into REQUEST_CHANGES like a fresh one. The record is removed whether it matched or not, so an approval is used at most once. A changed model, runner, prompt, configuration or Bureau version is not compared; run `python3 scripts/bureau-supervision.py resume TEAM-123` first to have the change judge the pull request.

See the [changelog](https://github.com/KaiaK808/bureau/blob/v3.0.1/CHANGELOG.md) for the full entries and the [exit codes](https://github.com/KaiaK808/bureau/blob/v3.0.1/docs/exit-codes.md) for the vocabulary.

## Upgrade an existing installation

From **v3.0.0 or v3.0.0-rc.2**: select tag `v3.0.1` in the source skill checkout and resync the scripts scope as one set. No configuration change is required. Two behaviours to expect afterwards: in an installation that merges automatically, the queue loop now alerts once an hour for a pull request whose merge gate stays blocked in Merge, where it used to stay silent; and wrappers or repository tests that read the merge stage's exit code must accept `2` and `25`.

From **v3.0.0-rc.1, v2.0.0 or a legacy copy**: follow the [v3 upgrade section](migration.md#upgrade-to-v3) with tag `v3.0.1`.

## Compatibility and rollback

Dependencies and platforms are unchanged (Python 3.9+, Bash 3.2+, Git 2.26+, jq, curl; Node.js 18+ for the shared scheduler; Linux and macOS). Configuration schema versions 1 and 2 stay supported, and the manifest stays at version `1`, so an older installer still reads it. To roll back to v3.0.0, point the source at tag `v3.0.0` and resync the scripts scope, or restore the private backup ([rollback](migration.md#rollback)); a stop record written by v3.0.1 carries an extra `verdict` field that v3.0.0 ignores. Rollback cannot undo commits, pull requests or Linear transitions made in between; the needs-human hold caveat of [v3.0.0](release-notes-v3.0.0.md#compatibility-and-rollback) still applies.

## Validation and known limitations

v3.0.1 is validated by the full Linux/macOS suite on the release commit, the installer fixtures and an independent verification pass per pull request in which each finding was reproduced before it was fixed: #26 passed with small follow-ups (documentation and a record left behind when the ticket detail could not be fingerprinted), #27 took two rounds, and both rounds checked that no path merges on a red or pending gate, in the first check and in the just-in-time recheck. Every new guard has a test that runs the real code with a negative control against v3.0.0 and was mutation-checked; the one surviving mutant in #26 is equivalent (a stop that is still requested is decided before the reuse call). **No live ticket has run on v3.0.1 yet;** the two live acceptances of v3.0.0 in the maintainer's pilot installation predate these changes, and the second of them is where both problems were seen.

Known limitations:

- With `merge_mode: "manual"`, nothing moves a ticket from Merge to Done after the merge by hand.
- A `manual` repository without a Merge state refuses code review with `24` and re-picks the same ticket every tick, alerting at most once an hour.
- A check that never starts (for example an offline self-hosted runner) keeps the queue at a quiet `2`; the shepherd waits its budget and then halts.
- The review stage's inline merge ignores the merge result and reports Done even when the merge did not go through (older than v3.0.1).
- Commits co-authored by Claude count as bureau commits when deciding whether the rebase stage can resolve a conflict; this changes `25` into `2` for such a pull request, never whether it merges.
- The shepherd does not read local needs-human holds; a run that names a held ticket ignores the hold (the stage's non-zero exit still stops it).
- The review build check uses three steps; the QA stage's further fallbacks are not used in review.
- The installer copies every file under `templates/scripts`, including files Git ignores; a `.env` placed there would be copied into the target repository (the source is then recorded as dirty).
- The alert throttle key has no repository in it, so two installations on one machine throttle each other's alerts.
- `pick_issue` and `count_in_flight_issues` still read a missing list as empty; Linear's schema does not allow that answer.
- A file that is already modified before `repo.post_implement_command` runs and that the command modifies further is not detected as left uncommitted.
