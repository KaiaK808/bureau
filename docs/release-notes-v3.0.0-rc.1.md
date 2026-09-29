# Bureau v3.0.0-rc.1 — hardening from the installations

**Release candidate · 2026-09-28 · source tag `v3.0.0-rc.1`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v3.0.0-rc.1) is marked as a prerelease and records the exact tagged commit. The stable release remains [v2.0.0](release-notes-v2.0.0.md) until v3.0.0 is published. This candidate is superseded by [v3.0.0-rc.2](release-notes-v3.0.0-rc.2.md) and the stable [v3.0.0](release-notes-v3.0.0.md) (patched by [v3.0.1](release-notes.md)). v3.0.0 follows once a pilot installation has been resynced with this candidate and has qualified one ticket end to end.

This is a major release because the exit-code contract between the stages and their drivers changed: a review BLOCK now ends the stage with `25` instead of `0`, Linear that stays unusable ends a stage with the new code `27`, and the shepherd halts with an alert on every code it does not treat as carry-on or retry. Drivers, wrappers and repository tests that read these codes need to be checked when upgrading.

## What changed

- **Hardening carried over from the installations** ([#11](https://github.com/KaiaK808/bureau/pull/11)): `.env` is parsed, never sourced; a red build can no longer soften a BLOCK; no CI suppressor reaches `main` through a squash message, and the implement and QA stages halt when one is in the squash range; Linear answers are retried and then classified as `linear-unusable` (`27`); labels resolve to the issue's own team; an npm project's `node_modules` is restored after the worktree reset (`24` on failure); a failed push is loud; the in-flight cap counts leaf issues of the configured projects; the spec cross-check runs under macOS `/bin/bash` 3.2 and distinguishes clean, conflicts and incomplete.
- **The installer records its source** ([#12](https://github.com/KaiaK808/bureau/pull/12)): an asset `--apply` writes the source tag, commit and dirty state per scope into `.bureau-install.json`, and doctor reports it as `template_source`. The manifest stays at version `1`.
- **The review build check uses `repo.test_command`** ([#13](https://github.com/KaiaK808/bureau/pull/13)): order `repo.test_command`, `scripts/bureau-test.sh`, `npm run build`; when none applies the review comment says "not checked" instead of "Passed".
- **Shepherd reads fail closed** ([#14](https://github.com/KaiaK808/bureau/pull/14)): a failed read of the ticket's state, labels or branch halts instead of reading as "no state", "no label" or "no branch"; `27` halts with the fault class, any other failure halts with `1` and `needs-human`, and a read cut short by Ctrl-C or SIGTERM is a cancelled run that writes nothing.
- **Merge policy as configuration** ([#15](https://github.com/KaiaK808/bureau/pull/15)): `agents.merge_mode: "manual"` switches the merge and rebase stages off for every dispatcher, parks an approved ticket in the Merge state and ends the shepherd there without an alert. `manual` requires a configured Merge state.

See the [changelog](../CHANGELOG.md) for the full entries and the [exit codes](exit-codes.md) for the vocabulary.

## Upgrade an existing installation

Select tag `v3.0.0-rc.1` in the actual source skill checkout using the [source-update procedure](migration.md#select-the-source-release), refresh skill discovery, and then resync each adopting repository with one pull request per repository. The [v3 upgrade section](migration.md#upgrade-to-v3) lists what to set **before** the resync:

- `agents.merge_mode: "manual"` where a human merges (and a Merge state in `linear.teams[0].states.merge`), because the resync replaces a hand-edited early `exit 2` in the merge and rebase scripts.
- `repo.test_command` where the review build check ran a local script, and a `.gitignore` that covers everything that command writes.
- The scripts scope lands as one set; an installation without a manifest sees every differing file as a conflict and needs a reviewed `--overwrite PATH` for each one.
- Repository tests that assert the old exit codes are updated in the same pull request.

Pause dispatch first, keep a private backup of the scripts, configuration, manifest and instruction files, run doctor after the resync and qualify one ticket before dispatch resumes.

## Compatibility and rollback

The runtime keeps its dependencies (Python 3.9+, Bash 3.2+, Git 2.26+, jq, curl; Node.js 18+ for the shared scheduler) and supports Linux and macOS. Configuration schema versions 1 and 2 stay supported; `agents.merge_mode` is optional and defaults to `auto`, which merges as before. The manifest stays at version `1`, so an older installer still reads it for a rollback. Rollback restores the private backup and points the source back at the recorded previous commit ([rollback](migration.md#rollback)); it cannot undo commits, pull requests or Linear transitions made in between.

## Validation and known limitations

Validated on the release commit by the full Linux/macOS suite (Bash syntax, ShellCheck, rendered documentation and every `tests/test_*.sh`), the installer fixtures (local edits stopping a batch and an explicit overwrite, managed instruction boundaries, a scoped resync preserving configuration, Spec Kit constitution preservation, source recording and manifests written by older installers), and one independent verification pass per pull request #12–#15 that reproduced each finding before it was fixed; the verification of #12 also ran the v2.0.0 installer against a manifest written by this one (it keeps working and carries the record along). Every guard added in #11–#15 has a test that runs the real code with a negative control, and was mutation-checked. **No adopting repository has run this candidate on a live ticket yet**; that is what the pilot adds before v3.0.0.

Known limitations:

- With `merge_mode: "manual"`, nothing moves a ticket from Merge to Done after the merge by hand; the merge stage refuses before it would notice the merged pull request.
- A `manual` repository without a Merge state refuses code review with `24` and re-picks the same ticket every tick, alerting at most once an hour, until the Merge state is configured or the mode is changed.
- The review build check uses three steps; the QA stage's further fallbacks (`npm test`, `cargo test`, `pytest`, `go test`) are not used in review.
- A ticket state that Linear reports as genuinely empty still makes the shepherd retry every 60 seconds without a limit.
- The installer copies every file under `templates/scripts`, including files Git ignores; a `.env` placed there would be copied into the target repository. The source is then recorded as dirty.
