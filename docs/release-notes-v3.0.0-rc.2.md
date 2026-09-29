# Bureau v3.0.0-rc.2 — findings from the first live acceptance

**Release candidate · 2026-09-28 · source tag `v3.0.0-rc.2`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.0.0-rc.2) is marked as a prerelease and records the exact tagged commit. This candidate is superseded by the stable [v3.0.0](release-notes-v3.0.0.md), which has the same runtime, and by the patch release [v3.0.1](release-notes.md). The previous candidate is [v3.0.0-rc.1](release-notes-v3.0.0-rc.1.md).

rc.2 carries the findings of the first live acceptance and of a check of seven tickets an installation had filed against its own pipeline scripts. It keeps rc.1's exit-code contract (a review BLOCK ends with `25`, `27` means Linear stayed unusable, the shepherd halts with an alert on every code except `0`, `2`, `10` and `16`) and adds to it: a failed `needs-human` write now ends a stage with `25`, a failed final implement push with `18`, a failed `repo.post_implement_command` with `14`, and an interrupted shepherd with `130`.

## What changed

- **Review verdict rules in one order** ([#18](https://github.com/KaiaK808/bureau/pull/18)): `decide_review_verdict` checks the verdict, the security count, the security specialist's CRITICAL count, the security floor, the build fold, and the cycle cap last. An APPROVE with a permanently red build now reaches the cap and escalates instead of looping; an unreadable or negative security count and a specialist CRITICAL end BLOCK; the text fallback accepts only an exact verdict word.
- **A needs-human escalation survives a failed label write** ([#17](https://github.com/KaiaK808/bureau/pull/17)): every stage goes through `mark_needs_human`. When the label cannot be written, the ticket is held locally under the configuration repository's git directory, the stage ends with `25` and the picker skips the ticket, retrying the label once per pick. `queue-loop.sh` reports a pick that fails with `27` with an hourly alert instead of reading it as an empty queue.
- **Linear answers checked in transport** ([#19](https://github.com/KaiaK808/bureau/pull/19)): every request has a time limit (30 s, connect 10 s); an HTTP status outside 2xx, `{"data":{}}`, a null root field and a NUL byte are unusable and go through the retry ladder to `27`; the issue readers require the lists they read.
- **Shepherd outside the stages** ([#20](https://github.com/KaiaK808/bureau/pull/20)): its own moves are guarded; a read after a move is confirmed (up to three reads, 5 s apart) so a moment-old answer no longer re-runs a stage; Ctrl-C or SIGTERM during a read, move, wait or stage ends the run as cancelled (`130`) after releasing the claim; five answers without a state in a row halt; a relative `--worktree` is resolved against the repository root.
- **`repo.post_implement_command` and a final push that cannot be lost** ([#21](https://github.com/KaiaK808/bureau/pull/21)): an optional, time-limited hook runs wherever the implement stage releases work for review, for repositories that derive files from an implementation; a failed final push is retried once and stops the hand-off with `18` when a fetch shows origin still lacks commits.
- **Installation names anonymised** ([#22](https://github.com/KaiaK808/bureau/pull/22)): documentation, template comments and tests refer to installations as A, B and C.
- **CI job limit** ([#23](https://github.com/KaiaK808/bureau/pull/23)): the test job may run 20 minutes (the macOS suite reached 10).

See the [changelog](../CHANGELOG.md) for the full entries and the [exit codes](exit-codes.md) for the vocabulary.

## Upgrade an existing installation

From **v3.0.0-rc.1**: select tag `v3.0.0-rc.2` in the source skill checkout and resync the scripts scope as one set. No configuration change is required. New optional settings: `repo.post_implement_command` (and `BUREAU_POST_IMPLEMENT_TIMEOUT`, default 900 s), `BUREAU_LINEAR_MAX_TIME` / `BUREAU_LINEAR_CONNECT_TIMEOUT` or `.linear.request.max_time` / `.connect_timeout`, and `BUREAU_SHEPHERD_CONFIRM_SECONDS` (environment only). A post-implement command must be idempotent and exit `0` when there is nothing to commit.

From **v2.0.0 or a legacy copy**: follow the [v3 upgrade section](migration.md#upgrade-to-v3) (set `agents.merge_mode: "manual"` with a Merge state where a human merges, `repo.test_command` where the review ran a local script, a `.gitignore` covering what that command writes, the scripts scope as one set with a reviewed `--overwrite` per differing file, and repository tests that assert old exit codes updated in the same pull request).

Behaviour to check after the resync: a missing or invalid Linear key now shows as `27` (hourly queue alert) rather than as an idle queue; an implement halt whose label could not be written ends with `25` and keeps unpushed work in its worktree for a human; a review whose security count cannot be read ends BLOCK.

## Compatibility and rollback

Dependencies and platforms are unchanged (Python 3.9+, Bash 3.2+, Git 2.26+, jq, curl; Node.js 18+ for the shared scheduler; Linux and macOS). Configuration schema versions 1 and 2 stay supported, and the manifest stays at version `1`, so an older installer still reads it. Rollback restores the private backup and points the source back at the previous tag ([rollback](migration.md#rollback)); it cannot undo commits, pull requests or Linear transitions made in between. A local needs-human hold is a file in `<git common dir>/bureau/needs-human-held/`; an older runtime ignores it, so roll back only after the held tickets carry their label or the hold files are removed.

## Validation and known limitations

**Live acceptance on rc.1:** the maintainer's pilot installation was resynced to rc.1 and drove one new ticket end to end with the shepherd — Triage → Spec → Spec Review → Build → QA → Build Review (one REQUEST_CHANGES round on a real gap) → Build → QA → Build Review (APPROVE, review build check ran the repository's test command) → merge gate (all gates passed, sanitised merge message, CI ran on `main`) → Done. It found two defects, both fixed in rc.2 (#20): a relative `--worktree` halted the shepherd with `128`, and a moment-old read after a stage re-ran a paid stage.

rc.2's own changes are validated by the full Linux/macOS suite on the release commit, the installer fixtures, and at least one independent verification pass per pull request #17–#21 in which each finding was reproduced before it was fixed; every new guard has a test that runs the real code with a negative control and was mutation-checked. The pilot is resynced to rc.2 next; **v3.0.0** follows after that.

Known limitations:

- With `merge_mode: "manual"`, nothing moves a ticket from Merge to Done after the merge by hand.
- A `manual` repository without a Merge state refuses code review with `24` and re-picks the same ticket every tick, alerting at most once an hour.
- The review build check uses three steps; the QA stage's further fallbacks are not used in review.
- The installer copies every file under `templates/scripts`, including files Git ignores; a `.env` placed there would be copied into the target repository (the source is then recorded as dirty).
- Resuming after a `--no-merge` stop runs the paid review again even when head and base are unchanged.
- The shepherd does not read local needs-human holds; a run that names a held ticket ignores the hold (the stage's non-zero exit still stops it).
- The alert throttle key has no repository in it, so two installations on one machine throttle each other's alerts.
- `pick_issue` and `count_in_flight_issues` still read a missing list as empty; Linear's schema does not allow that answer.
- A file that is already modified before `repo.post_implement_command` runs and that the command modifies further is not detected as left uncommitted.
