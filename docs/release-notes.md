# Bureau v3.0.0 — the exit-code contract, merge policy and a pipeline that fails closed

**Stable release · 2026-09-28 · source tag `v3.0.0`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.0.0) records the exact tagged commit. v3.0.0 has the same runtime, installer, templates and tests as its second candidate, [v3.0.0-rc.2](release-notes-v3.0.0-rc.2.md); the first candidate was [v3.0.0-rc.1](release-notes-v3.0.0-rc.1.md). The previous stable release is [v2.0.0](release-notes-v2.0.0.md).

This is a major release because the exit-code contract between the stages and whatever drives them (the shepherd, the queue loop, ticks, wrappers and repository tests) changed. Anything that reads these codes needs to be checked when upgrading.

## What changed since v2.0.0

**The exit-code contract.** A review BLOCK ends the review stage with `25` instead of `0`. Linear that stays unusable after the retry ladder ends a stage with the new code `27` (`linear-unusable`). `24` (`environment-blocked`) gains two uses: a failed dependency restore after the worktree reset, and `agents.merge_mode: "manual"` without a Merge state. The shepherd halts with an alert on every code except `0` and `2` (carry on) and `10` and `16` (retry), and an asked-for stop before the merge (`20` under `--no-merge` or `merge_mode: "manual"`) stays quiet; a run interrupted by Ctrl-C or SIGTERM during a read, move, wait or stage ends as a cancelled run (`130`) and writes nothing to Linear. A stage whose `needs-human` label cannot be written ends with `25`, a failed final implement push with `18` when origin still lacks commits, and a failed `repo.post_implement_command` with `14`. The vocabulary is in [exit codes](exit-codes.md).

- **Hardening carried over from the installations** ([#11](https://github.com/KaiaK808/bureau/pull/11)): `.env` is parsed, never sourced; a red build can no longer soften a BLOCK; no CI suppressor reaches `main` through a squash message, and the implement and QA stages halt when one is in the squash range; Linear answers are retried and then classified; labels resolve to the issue's own team; an npm project's `node_modules` is restored after the worktree reset; a failed push is loud; the in-flight cap counts leaf issues of the configured projects; the spec cross-check runs under macOS `/bin/bash` 3.2.
- **The installer records its source** ([#12](https://github.com/KaiaK808/bureau/pull/12)): an asset `--apply` writes the source tag, commit and dirty state per scope into `.bureau-install.json`, and doctor reports it as `template_source` (`recorded`, `source not recorded` or `stale`). The manifest stays at version `1`.
- **The review build check uses `repo.test_command`** ([#13](https://github.com/KaiaK808/bureau/pull/13)): order `repo.test_command`, `scripts/bureau-test.sh`, `npm run build`; when none applies the review comment says "not checked" instead of "Passed".
- **Shepherd reads fail closed** ([#14](https://github.com/KaiaK808/bureau/pull/14)): a failed read of the ticket's state, labels or branch halts instead of reading as "no state", "no label" or "no branch".
- **Merge policy as configuration** ([#15](https://github.com/KaiaK808/bureau/pull/15)): `agents.merge_mode: "manual"` switches the merge and rebase stages off for every dispatcher, parks an approved ticket in the Merge state and ends the shepherd there without an alert; it requires a configured Merge state, and any value other than absent, `null` or `"auto"` falls closed to manual.
- **A needs-human escalation survives a failed label write** ([#17](https://github.com/KaiaK808/bureau/pull/17)): every stage goes through `mark_needs_human`; when the label cannot be written, the ticket is held locally under the configuration repository's git directory and the picker skips it, retrying the label once per pick. `queue-loop.sh` reports a pick that fails with `27` with an hourly alert instead of reading it as an empty queue.
- **Review verdict rules in one order** ([#18](https://github.com/KaiaK808/bureau/pull/18)): `decide_review_verdict` checks the verdict, the security count, the security specialist's CRITICAL count, the security floor, the build fold, and the cycle cap last; an unreadable security count and a specialist CRITICAL end BLOCK, and the text fallback accepts only an exact verdict word.
- **Linear answers checked in transport** ([#19](https://github.com/KaiaK808/bureau/pull/19)): every request has a time limit (30 s, connect 10 s); an HTTP status outside 2xx, `{"data":{}}`, a null root field and a NUL byte are unusable; the issue readers require the lists they read.
- **Shepherd outside the stages** ([#20](https://github.com/KaiaK808/bureau/pull/20)): guarded moves, a confirmed read after each move (up to three reads, 5 s apart), cancellation on signals, a bound on answers without a state, and a relative `--worktree` resolved against the repository root.
- **`repo.post_implement_command` and a final push that cannot be lost** ([#21](https://github.com/KaiaK808/bureau/pull/21)): an optional, time-limited, idempotent hook runs wherever the implement stage releases work for review, for repositories that derive files from an implementation; a failed final push is retried once and stops the hand-off with `18` when a fetch shows origin still lacks commits.
- **Installation names anonymised** ([#22](https://github.com/KaiaK808/bureau/pull/22)) and **CI job limit raised to 20 minutes** ([#23](https://github.com/KaiaK808/bureau/pull/23)).

The [changelog](../CHANGELOG.md) has the full entries of both candidates.

## Upgrade an existing installation

From **v3.0.0-rc.2**: select tag `v3.0.0` in the source skill checkout; no resync is needed, because the runtime is identical.

From **v3.0.0-rc.1**: select tag `v3.0.0` and resync the scripts scope as one set. No configuration change is required; the new optional settings are listed in the [v3 upgrade section](migration.md#upgrade-to-v3).

From **v2.0.0 or a legacy copy**: follow the [v3 upgrade section](migration.md#upgrade-to-v3). Set `agents.merge_mode: "manual"` with a Merge state where a human merges, `repo.test_command` where the review ran a local script, and a `.gitignore` covering what that command writes; resync the scripts scope as one set with a reviewed `--overwrite` per differing file; update repository tests that assert old exit codes in the same pull request. Pause dispatch first, keep a private backup of the scripts, configuration, manifest and instruction files, run doctor after the resync and qualify one ticket before dispatch resumes.

## Compatibility and rollback

Dependencies and platforms are unchanged (Python 3.9+, Bash 3.2+, Git 2.26+, jq, curl; Node.js 18+ for the shared scheduler; Linux and macOS). Configuration schema versions 1 and 2 stay supported, and the manifest stays at version `1`, so an older installer still reads it. Rollback restores the private backup and points the source back at the previous tag ([rollback](migration.md#rollback)); it cannot undo commits, pull requests or Linear transitions made in between. A local needs-human hold is a file in `<git common dir>/bureau/needs-human-held/`; an older runtime ignores it, so roll back only after the held tickets carry their label or the hold files are removed.

## Validation and known limitations

**Live acceptance, twice, in the maintainer's pilot installation.** On rc.1 the shepherd drove one ticket from Triage through Spec, Spec Review, Build, QA and Build Review (one round of requested changes on a real gap) to the merge gate and Done; it found two defects, both fixed in rc.2. On rc.2 a second ticket went Triage → Spec → Spec Review → Build, where `repo.post_implement_command` regenerated the derived files in its own `Bureau-Generated` commit before the hand-off → QA → Build Review (APPROVE; the review build check ran the repository's test command) → merge gate, which correctly refused the pull request while its CI was red for an unrelated flaky test in the pilot and merged it after a green rerun → Done. That run used a relative `--worktree`.

The changes are also validated by the full Linux/macOS suite on the release commit, the installer fixtures (local edits stopping a batch, explicit overwrites, managed instruction boundaries, scoped resyncs preserving configuration, Spec Kit constitution preservation, source recording and manifests written by older installers), and at least one independent verification pass per pull request #12–#21 in which each finding was reproduced before it was fixed. Every guard added in #11–#21 has a test that runs the real code with a negative control and was mutation-checked.

Known limitations:

- With `merge_mode: "manual"`, nothing moves a ticket from Merge to Done after the merge by hand.
- A `manual` repository without a Merge state refuses code review with `24` and re-picks the same ticket every tick, alerting at most once an hour.
- When the merge gate reports a pull request as not eligible (for example red CI), the merge stage ends with `0`; the shepherd's confirmation then re-reads and prints "still reads 'Merge' after the move" before the stuck detector halts with `needs-human`. The outcome is correct, the message is misleading.
- Resuming after a `--no-merge` stop runs the paid review again even when head and base are unchanged.
- The shepherd does not read local needs-human holds; a run that names a held ticket ignores the hold (the stage's non-zero exit still stops it).
- The review build check uses three steps; the QA stage's further fallbacks are not used in review.
- The installer copies every file under `templates/scripts`, including files Git ignores; a `.env` placed there would be copied into the target repository (the source is then recorded as dirty).
- The alert throttle key has no repository in it, so two installations on one machine throttle each other's alerts.
- `pick_issue` and `count_in_flight_issues` still read a missing list as empty; Linear's schema does not allow that answer.
- A file that is already modified before `repo.post_implement_command` runs and that the command modifies further is not detected as left uncommitted.
