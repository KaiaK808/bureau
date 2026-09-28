# Upgrade, migration and rollback

An upgrade has two steps: update the **Bureau source skill**, then resync its assets into **each adopting repository**. Updating the source clone alone leaves installed scripts and commands unchanged. `/bureau-init --update` edits configuration; it does not upgrade installed assets.

This guide upgrades installations from the official [KaiaK808/bureau](https://github.com/KaiaK808/bureau) source: legacy untagged installations and v2.0.0 installations to the stable **Bureau v2.0.0** or to the release candidate **Bureau v3.0.0-rc.1**. The commands below require the selected release's source skill. See the [v3.0.0-rc.1 release](https://github.com/KaiaK808/bureau/releases/tag/v3.0.0-rc.1) and its [release notes](release-notes.md), the [v2.0.0 release](https://github.com/KaiaK808/bureau/releases/tag/v2.0.0) and its [release notes](release-notes-v2.0.0.md), the [changelog](../CHANGELOG.md) and the [release process](releases.md). A Bureau major version marks operational changes (v2.0.0: explicit worker ownership; v3: the exit-code contract, see [Upgrade to v3](#upgrade-to-v3)); it does not require configuration schema v2.

## Select the source release

First update the actual source clone that supplies `/bureau-init`. Its usual location is `~/.claude/skills/bureau-init`, but it may be a symlink or installed elsewhere. Check the loaded skill location; do not pull the adopting project's repository by mistake.

In a terminal, inspect the source checkout:

```sh
BUREAU_SOURCE="$HOME/.claude/skills/bureau-init" # replace with the actual loaded skill path
git -C "$BUREAU_SOURCE" status --short --branch
git -C "$BUREAU_SOURCE" remote -v
git -C "$BUREAU_SOURCE" describe --tags --always --dirty
git -C "$BUREAU_SOURCE" rev-parse HEAD
```

For a Codex-only install, the entry point may be `~/.agents/skills/bureau-init` or a configured skill directory. Follow its link to the actual source clone. Record the previous commit and branch/tag privately for rollback; `describe` alone may name a nearby tag instead of the installed commit.

Confirm that `origin` identifies the official `KaiaK808/bureau` repository (HTTPS or SSH). Preserve local source changes before continuing; do not reset the skill clone. With a clean checkout, select the exact release (`v2.0.0` for the stable release, `v3.0.0-rc.1` for the candidate):

```sh
BUREAU_RELEASE=v3.0.0-rc.1 # or v2.0.0
git -C "$BUREAU_SOURCE" fetch origin tag "$BUREAU_RELEASE" &&
git -C "$BUREAU_SOURCE" switch --detach "refs/tags/$BUREAU_RELEASE" &&
git -C "$BUREAU_SOURCE" rev-parse HEAD
```

Stop if fetching fails, especially if an existing local tag conflicts with the remote; do not force-replace it. Compare the resulting commit with the commit recorded in the GitHub Release and record it for rollback. This leaves the source on a detached release checkout. A later release upgrade repeats these steps with that release's tag; it does not use `git pull`.

An intentionally `main`-tracking installation can instead use `git pull --ff-only` when its checkout is clean and its upstream is the official `origin/main`. This follows ongoing development rather than pinning a release. If the source is an archive-origin checkout, an unrelated history or a plain copied directory, keep it intact and install a separate official clone from the [installation guide](../README.md#install); review how the loaded skill entry point should move before replacing any link. Do not merge unrelated histories or change the old checkout's remote as an upgrade shortcut.

## Upgrade to v3

v3 changes the exit-code contract between the stages and whatever drives them (the shepherd, the queue loop, ticks, wrappers and repository tests). Check anything that reads these codes before resuming dispatch:

| Situation | v2.0.0 | v3 |
|---|---|---|
| Review ends with BLOCK or an unknown verdict | `0` | `25` (`needs-human-or-paused`) |
| Linear stays unusable after the retries (no answer, not JSON, GraphQL errors, no data) | no dedicated code; some reads failed open | `27` (`linear-unusable`), new |
| An npm project's dependencies cannot be restored after the worktree reset | no restore; the build ran red | `24` (`environment-blocked`, an existing code) |
| `agents.merge_mode` is `manual` without a Merge state | not applicable | code review refuses at its start with `24` |
| Shepherd receives `22` to `26` from a stage | stopped without an alert | halts with an alert; only `0` and `2` carry on, `10` and `16` retry |
| Shepherd receives a `20` that `--no-merge` or `BUREAU_NO_MERGE` asked for | halted with an alert | stops quietly; any other `20` still alerts |
| The shepherd's own read of state, labels or branch fails | read as "no state", "no label" or "no branch" | `27` halts with the fault class, another failure halts with `1` and `needs-human`, Ctrl-C or SIGTERM is a cancelled run (`130`) that writes nothing |

Some installations gave `20`, `21` or `25` local meanings. Rewrite local changes onto the codes in [exit codes](exit-codes.md) instead of copying a code across.

Upgrade one adopting repository at a time, each in its own pull request:

1. **Pause dispatch** and let nothing be in flight. Push any unpushed commit to a branch first.
2. **Branch from the repository's `main` in a fresh worktree**, so dirty files and other branches stay out of the change. Keep a private backup of `scripts/`, `.bureau.json`, `.bureau-install.json` and the instruction files (see [before changing an adopting repository](#before-changing-an-adopting-repository)).
3. **Pin the source** to the release tag as in [select the source release](#select-the-source-release).
4. **Set the configuration the new scripts expect, before the resync:**
   - Where a human merges, set `"agents": {"merge_mode": "manual"}` and make sure `linear.teams[0].states.merge` is set. The resync replaces a hand-edited early `exit` in `merge-pipeline.sh` or `rebase-pipeline.sh`; without the key, automatic merging is on again. See the [recipe](recipes.md#merge-by-hand).
   - Where the review build check ran a local script, set `repo.test_command` to it. `scripts/bureau-test.sh` is found without configuration.
   - Make `.gitignore` cover everything `repo.test_command` writes, such as `__pycache__/` or build output. Otherwise the review leaves untracked files, the worker keeps the worktree as unfinished work, and the next reset refuses with `21`.
5. **Preview the scripts scope and apply it as one set.** `bureau-config.sh` loads `bureau-env.sh`, `merge-pipeline.sh` loads `merge-body.sh`, and `squash-marker-check.sh` reads `ci-skip-markers.txt`; a partial scripts scope is not a working runtime. An installation without `.bureau-install.json` sees every differing file as a conflict. For each one, diff the local file against `templates/scripts/FILE` and decide: take the template (the usual answer where the local hardening is now upstream), express a local policy as configuration, or keep a genuine local need in a separate local file. Then apply with the reviewed `--overwrite` list ([preview and resolve](#preview-and-resolve-asset-conflicts)). Add the interfaces scope only where the repository uses the commands.
6. **Optionally migrate the configuration** ([configuration migration](#optional-configuration-migration)).
7. **Update or retire repository tests** that assert the old pipeline behaviour (exit codes, file layout) in the same pull request.
8. **Verify:** doctor (`python3 scripts/bureau-doctor.py --mode background`), the repository's CI, then qualify one ticket end to end with the shepherd before dispatch resumes. Name the release tag, every carried local change, the preview before and after, and a reason for each `--overwrite` in the pull request.
9. **Rollback** restores the backup and points the source back at the previous commit ([rollback](#rollback)). The manifest stays at version `1`, so an older installer still reads it.

## Resync in Claude Code or Codex

Refresh the assistant's skill discovery or start a new session after changing the source. Complete the [pre-upgrade checks](#before-changing-an-adopting-repository), including pausing dispatch and preserving local files. Then, **inside each adopting repository**, request the desired scope in Claude Code:

```text
/bureau-init --resync-interfaces --resync-scripts --target both
```

In a Codex app task, use:

```text
$bureau-init --resync-interfaces --resync-scripts --target both
```

This requests both Claude and Codex interfaces plus the shared runtime. To remain Claude-only, use `--target claude`. It does not switch the background model provider. Refresh existing Spec Kit assets as a separate operation:

```text
/bureau-init --resync-speckit --target both
```

If the repo uses Bureau's installed Claude planning workflows, refresh those too:

```text
/bureau-init --resync-workflows --target both
```

Use the same target selection as above, and replace `/bureau-init` with `$bureau-init` when requesting these scopes in Codex. Scripts/interfaces do not implicitly update `.claude/workflows/`. Existing project CI is separate; use `--resync-ci` only when a CI scaffold change is requested.

Existing active Spec Kit integration stays active. Add `--active-integration codex` only when you intend to switch it. Spec Kit resync refreshes all discovered installed integrations as well as the requested target, so selecting one target does not exclude the other host's existing Spec Kit assets. It requires the pinned `specify-cli` 0.7.5. Its customization/constitution preservation is separate from Bureau's asset conflict handling.

For a previously installed Git extension, the resync also installs missing native Codex hook skills and records their commands while preserving existing skills, hook configuration and other host registrations. Core initialization alone did not perform this registration in older Bureau candidates. Refresh interfaces too for native hook-name mapping and the helper guidance below.

Retained Spec Kit scripts may use different feature-selection rules from newly generated scripts. Before a plan/task helper runs on a `codex/*` or detached checkout, resolve the issue's exact approved spec folder and pass both variables:

```sh
SPECIFY_FEATURE=NNN-approved-feature \
SPECIFY_FEATURE_DIRECTORY=specs/NNN-approved-feature \
bash .specify/scripts/bash/check-prerequisites.sh --json --paths-only
```

Replace both example values with the real approved feature and configured specs path. Use the same pair for other helpers. `SPECIFY_FEATURE` alone can still follow stale `feature.json` in newer scripts; older retained scripts may ignore that file altogether. Resolve duplicate prefixes explicitly and preserve the actual issue branch.

For an older installation with local changes, append this instruction to the resync request:

> Preview the selected upgrade scopes and compare each conflict with the incoming files. Preserve my local changes and user instructions. Back up conflicting originals privately; show the proposed per-file resolution before applying a replacement that has not already been authorized. Keep background workers paused until the complete runtime and any retained customizations have been checked. Report remaining intentional drift.

The slash commands are requests interpreted by the loaded Bureau skill. The Python helper is the deterministic interface for previews and writes; the command does not itself promise an automatic merge of customizations.

## Before changing an adopting repository

Check the [dependencies](../README.md#install) before using the new helper: Python 3.9+, Bash 3.2+, Git 2.26+, jq and curl. Spec Kit refresh requires 0.7.5; the shared scheduler needs Node.js 18+. Background stages need their selected provider CLIs and GitHub operations need `gh`. Current Codex app tasks do not require background provider CLIs or tmux.

1. Inspect `git status`, the current branch and active worktrees. Record the installed source revision: doctor reports it as `template_source` for installs applied by a helper that records it (see below); older installs report `source not recorded`, so take it from your own notes if known. An old install may have no `.bureau-install.json`; absence of hashes is expected and must not be treated as permission to overwrite.
2. Stop new dispatch and let running stages finish. With the new runtime, use `python3 scripts/bureau-runtime.py pause` and inspect `status`. On an older install, stop its launcher/scheduler normally. A stopped launcher does not prove an existing worker has exited.
3. Preserve tracked changes, untracked work and local customizations. Before applying asset scopes, use their previews to inventory affected paths and keep an exact private backup of all existing assets in those scopes, even files without conflicts. Include project instruction files, `.bureau-install.json`, `.gitignore`, configuration, credentials, and selected workflow/CI assets. Spec Kit preview lists commands rather than every file: before its resync, also back up the complete `.specify` tree and the project's `.claude` and `.agents` integration trees, including `speckit-*` skills for every discovered host. Record which paths did not exist so rollback can identify additions. Keep these backups outside the tracked tree. Git commits/diffs alone do not preserve ignored or untracked files; configuration such as `.bureau.json` may be tracked or ignored depending on the project. Never put credentials into a commit, PR or public support log.
4. Do not reset or detach existing worker/app checkouts. The new runtime deliberately refuses to treat old unregistered `.worktrees/` directories as disposable. Finish, inspect and retire or adopt them before reuse; see the [ownership/recovery protocol](stage-protocol.md).

## Preview and resolve asset conflicts

Run the helper from the refreshed source, with the adopting repository as the current directory:

```sh
python3 "$BUREAU_SOURCE/scripts/bureau_install.py" assets \
  --repo "$PWD" --target both --scope interfaces --scope scripts
```

Preview writes nothing. Exit code `3` means at least one asset conflict. Even with `--apply`, **one unresolved conflict prevents every file in that asset batch from being written**, including new runtime helpers and the manifest. Configuration migration and Spec Kit initialization are separate operations, not part of this transaction.

The preview also prints `source`: the revision of the Bureau source it would install from, with the exact tag if its commit is tagged (`tag`, otherwise `null`), the nearest `git describe --tags` (`describe`), the full `commit`, and `dirty`. Check it before `--apply`, for example that it names the release you pinned. A successful `--apply` records that value in `.bureau-install.json` under `sources`, keyed by what the batch wrote (`scripts`, `ci`, `workflows`, `interfaces/claude`, `interfaces/codex`), next to `sources_files_sha256`, a digest of the recorded file hashes. A later partial apply relabels only the keys it wrote, so a scripts scope installed from one release and a CI scope from another stay distinguishable. When the incoming digest no longer matches the file hashes, the record was carried along by a writer that changed files without recording its source; the apply then drops the whole carried record and keeps only the keys it wrote. `dirty` is true when the source checkout reports modified or untracked files, or when any file this batch reads (the templates it installs and the installer itself) is not byte-identical to its blob at the recorded commit, or when either cannot be read. The second check matters because the installer copies every file in `templates/scripts`, including files Git ignores such as `.env`, `.DS_Store` or `*.log`, which `git status` never shows. Keep such files out of `templates/`: they are installed into the target repository. A source that is not its own Git checkout, such as an unpacked archive or a copy inside another repository, is recorded as `{"git": false, "note": "not a git checkout"}`; the enclosing repository's commit is never used. A preview or an apply stopped by a conflict writes no source. The manifest stays at version `1`: installers without source recording still read it.

```json
"sources": {
  "scripts": {"git": true, "tag": "v3.0.0", "describe": "v3.0.0", "commit": "<40-character SHA>", "dirty": false}
},
"sources_files_sha256": "<sha256 of the files map>"
```

| Preview result | Meaning | Resolution |
|---|---|---|
| `install` / `update` / `unchanged` | New file, unchanged installed baseline, identical content, or an explicitly selected replacement | Review the batch and apply when its conflicts are resolved |
| Existing file differs, no stored hash | The installer cannot distinguish an old generated file from local edits | Compare it with incoming content; preserve anything custom |
| Existing file differs from its stored baseline | Local customization or drift | Preserve it and review the incoming change |
| Legacy or malformed instruction markers | The managed part of CLAUDE.md/AGENTS.md cannot be identified safely | Review the exact generated region and fix its delimiters before retrying |

For plain scripts, compare `scripts/FILE.sh` against `$BUREAU_SOURCE/templates/scripts/FILE.sh`. Codex skills and scheduling workflows are rendered from templates, so compare the actual intended output, not an assumed identical template. You can install the same selected asset scopes into a throwaway Git repository to inspect the generated files. Never copy this source repo's maintenance CLAUDE.md or AGENTS.md into an adopting project as the incoming instruction file. If the old source revision is known, use it as a comparison base; without it, do not claim a reliable three-way merge.

Choose a resolution for **each** conflict:

- **Take upstream:** after reviewing and backing up the old file, name that specific path with `--overwrite`. Review the new preview, then repeat with `--apply`.
- **Keep custom behavior and upgrade:** preserve the original plus the intended custom changes, review the combined result, install the coherent upstream batch using the individually authorized replacements, then selectively reapply those custom changes. Keep dispatch stopped throughout. Do not copy the entire old file back over the new runtime.
- **Defer:** leave the batch unapplied until the conflict can be reconciled. An independent scope can be handled separately, but do not describe an incomplete scripts upgrade as a working new runtime.

Example for a single reviewed script replacement:

```sh
python3 "$BUREAU_SOURCE/scripts/bureau_install.py" assets \
  --repo "$PWD" --target both --scope interfaces --scope scripts \
  --overwrite scripts/qa-pipeline.sh
# After reviewing this preview, repeat the same command with --apply.
```

Other conflicts still block that example. Repeat `--overwrite` only for each reviewed path in the selected scope. There is no blanket force, skip-conflicts or automatic merge option.

For instruction files, Bureau manages only the region from `<!-- bureau-init:begin -->` to `<!-- bureau-init:end -->`. Text outside it is preserved. An older `<!-- bureau-init managed -->` section must be identified and delimited manually without enclosing unrelated user guidance. Malformed/legacy boundaries must be repaired first; `--overwrite CLAUDE.md` is not a bypass for invalid markers.

After reapplying customizations, preview may correctly report those paths as conflicts again. Record this as intentional drift and verify the resulting runtime. Do not edit `.bureau-install.json` to disguise the drift: its hashes describe the upstream baseline and protect those customizations at the next upgrade.

## Optional configuration migration

Version 1 configurations remain supported; resync does not replace `.bureau.json` or `.env`. Configuration version `2` is a schema version, not a Bureau release number. To adopt it, preview and then apply:

```sh
python3 "$BUREAU_SOURCE/scripts/bureau_install.py" migrate --repo "$PWD"
python3 "$BUREAU_SOURCE/scripts/bureau_install.py" migrate --repo "$PWD" --apply
```

Migration retains existing IDs, labels, models, switches and extension keys. It makes the existing Claude runner default explicit when absent and adds `agents.model_compatibility: "v1"` when migrating v1 unless compatibility was explicitly selected already. This preserves legacy model ownership: Codex uses Codex-specific settings instead of generic Claude defaults. A repeat migration makes no further changes.

The exact original config is saved with private permissions under `<shared-git-dir>/bureau/config-backups/.bureau.json.pre-v2.<unique>.bak`. This backup covers the configuration only; it does not back up scripts, `.env`, worktrees or external ticket state. Invalid/future config versions are rejected without replacement.

Installing Codex interfaces does not enable Codex background execution. To opt in, configure the desired default/stage runners, Codex-specific model settings and `repo.test_command`. To adopt v2 generic model semantics, first review or relocate legacy Claude model fields, then explicitly set `agents.model_compatibility` to `v2`.

## What changes for existing users

This table describes the change to v2.0.0; v3 is described in [Upgrade to v3](#upgrade-to-v3).

| Area | Previous installations | This update |
|---|---|---|
| Interactive use | Primarily Claude commands | Claude commands remain; Codex gains native skills, AGENTS.md and the current-task `$bureau` operator |
| Resync | Generated/copied files, often without an installation baseline | Preview, hashes, per-file conflicts and managed instruction boundaries |
| Background models | Claude-centered execution and limited Codex paths | Shared provider adapter, per-stage runners, structured results and separate execution evidence |
| Worker ownership | Legacy worktree conventions | Explicit registration/claims; existing user or unregistered checkouts refuse automatic reset |
| Cancellation | Shell exit could hide surviving work | Interrupted ownership is retained; unfinished work requires inspected recovery |
| Review boundary | Continuous pipeline could merge after approval | That mode still uses merge gates; app review and bounded ticks stop before merge by default |
| Supervision | Repeated polling could revisit approved/parked work | Durable review stops and human-blocker notifications; explicit resume/new inputs reopen review |
| Models/configuration | Generic model fields historically meant Claude | Legacy meaning retained through migration; v2 semantics require an explicit choice |
| QA/spec-review/copy publication | New files or rejected pushes could be missed | New files are published, push failures stop advancement, staged private files refuse publication |

The [changelog](../CHANGELOG.md) links the individual changes. Custom scripts that call internal helpers should be reviewed against the complete new runtime rather than mixed with selected old scripts.

## Verify before resuming

Run `python3 scripts/bureau-doctor.py --mode app`, or `--mode background` for the pipeline, then run the adopting project's configured tests. Inspect installed interfaces, `.specify/integration.json`, the final Git diff, retained customizations and run ownership. Check Bash syntax for installed shell scripts. A successful doctor is not a live workflow test.

Doctor is read-only: it reports configuration provenance, effective settings, real Bureau interfaces, managed drift and the recorded template source. `template_source.status` is `recorded` (with the per-scope `sources`), `source not recorded` when there is no manifest, the manifest predates source recording, or no scope is recorded yet (not an error; the next asset `--apply` that installs files records it), or `stale` with a warning when the file hashes changed after the record was written. That is what an installer without source recording leaves behind: it rewrites the hashes and carries the old record along. It resolves JSON and the process environment; it does not execute `.env`, authenticate providers, call Linear/models or run tests. Source trusted overrides before invoking it when needed. App mode does not require background CLIs; background GitHub stages require `gh`. The runtime uses the first configured Linear team's state map; doctor warns on multiple teams.

Qualify a representative ticket through the intended app/background path with the review-only boundary before enabling unattended dispatch. Authenticate selected providers normally. Resume workers/automations only when requested and the upgrade is coherent. See the [acceptance record](codex-acceptance.md) for what has and has not been exercised.

## Rollback

Stop new dispatch and inspect active owners before restoring anything. Preserve work created since the upgrade. Restore the recorded previous source commit/tag in a clean source checkout; for example, `git -C "$BUREAU_SOURCE" switch --detach PREVIOUS_COMMIT`, replacing the placeholder with the recorded commit. Restore the coherent pre-upgrade project asset set, instruction files and installer bookkeeping from the private backup, then reconcile subsequent local changes. Identify files newly installed by the upgrade and remove only confirmed upgrade additions that are absent from the old baseline and contain no later work.

If the selected older source supports deterministic previews, use a scoped preview to verify its baseline and review every conflict. An older helper does not record its source and carries the existing record along. Doctor reports that record as `stale` only once the older helper has changed a file hash; an apply that changed nothing leaves it `recorded`, which is still true of the files. After a restore of the pre-upgrade manifest from the backup it reports `source not recorded`. The next asset `--apply` by a recording helper drops a stale record and records only the scopes that apply wrote. A legacy source may have no installer helper or manifest support: do not assume the current resync commands work there, or mix selected old scripts with the new runtime. Restore customized behavior from its matching backup. A configuration backup covers configuration only and can be restored after comparing subsequent edits and runtime compatibility. Keep `.env` private and intact. Run the restored project's tests and available diagnostics before considering a restart.

Restoring source/configuration does not undo commits, Linear transitions or already-completed work. Preserve run records, provider logs, issue branches and checkpoints. Older runtimes may not honor new ownership records, so never restart one against unfinished work managed by the newer runtime. Do not delete leases or reset worktrees as a rollback shortcut.
