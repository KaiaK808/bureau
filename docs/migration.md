# Upgrade, migration and rollback

An upgrade has two steps: update the **Bureau source skill**, then resync its assets into **each adopting repository**. Updating the source clone alone leaves installed scripts and commands unchanged. `/bureau-init --update` edits configuration; it does not upgrade installed assets.

The Claude/Codex changes in this source tree are **Unreleased**. No versioned tag or GitHub Release has been published. Use the official [KaiaK808/bureau](https://github.com/KaiaK808/bureau) source; users following `main` receive these changes after their integration PR is merged. The commands below require that newer source skill. See the [changelog](../CHANGELOG.md), [draft release notes](release-notes.md) and [release process](releases.md).

## Quick path in Claude Code

First update the actual source clone that supplies `/bureau-init`. Its usual location is `~/.claude/skills/bureau-init`, but it may be a symlink or installed elsewhere. Check the loaded skill location; do not pull the adopting project's repository by mistake.

In a terminal, inspect the source checkout:

```sh
BUREAU_SOURCE="$HOME/.claude/skills/bureau-init" # replace if installed elsewhere
git -C "$BUREAU_SOURCE" status --short --branch
git -C "$BUREAU_SOURCE" remote -v
git -C "$BUREAU_SOURCE" describe --tags --always --dirty
```

Confirm that `origin` identifies the official `KaiaK808/bureau` repository (HTTPS or SSH). If the checkout is clean and tracks its `main` branch, update it with:

```sh
git -C "$BUREAU_SOURCE" pull --ff-only
```

Preserve local source edits and resolve source-level Git conflicts separately. Do not reset the skill clone to force an update. For a tagged release, fetch tags and select the exact published tag instead of following `main`; a detached release checkout does not use `git pull`. Record the old and new source commit/tag for rollback.

Refresh Claude Code's skill discovery or start a new session after changing the source. Then, **inside each adopting repository**, request the desired scope:

```text
/bureau-init --resync-interfaces --resync-scripts --target both
```

This requests both Claude and Codex interfaces plus the shared runtime. To remain Claude-only, use `--target claude`. It does not switch the background model provider. Refresh existing Spec Kit assets as a separate operation:

```text
/bureau-init --resync-speckit --target both
```

If the repo uses Bureau's installed Claude planning workflows, refresh those too:

```text
/bureau-init --resync-workflows --target both
```

Use the same target selection as above. Scripts/interfaces do not implicitly update `.claude/workflows/`. Existing project CI is separate; use `--resync-ci` only when a CI scaffold change is requested.

Existing active Spec Kit integration stays active. Add `--active-integration codex` only when you intend to switch it. Spec Kit resync requires the pinned `specify-cli` 0.7.5. Its customization/constitution preservation is separate from Bureau's asset conflict handling.

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

1. Inspect `git status`, the current branch and active worktrees. Record the installed source revision if known. An old install may have no `.bureau-install.json`; absence of hashes is expected and must not be treated as permission to overwrite.
2. Stop new dispatch and let running stages finish. With the new runtime, use `python3 scripts/bureau-runtime.py pause` and inspect `status`. On an older install, stop its launcher/scheduler normally. A stopped launcher does not prove an existing worker has exited.
3. Preserve tracked changes, untracked work and local customizations. Keep exact private copies of configuration, credentials and conflicting assets outside the tracked tree. Git commits/diffs alone do not back up ignored `.env`, `.bureau.json` or untracked files. Never put credentials into a commit, PR or public support log.
4. Do not reset or detach existing worker/app checkouts. The new runtime deliberately refuses to treat old unregistered `.worktrees/` directories as disposable. Finish, inspect and retire or adopt them before reuse; see the [ownership/recovery protocol](stage-protocol.md).

## Preview and resolve asset conflicts

Run the helper from the refreshed source, with the adopting repository as the current directory:

```sh
python3 "$BUREAU_SOURCE/scripts/bureau_install.py" assets \
  --repo "$PWD" --target both --scope interfaces --scope scripts
```

Preview writes nothing. Exit code `3` means at least one asset conflict. Even with `--apply`, **one unresolved conflict prevents every file in that asset batch from being written**, including new runtime helpers and the manifest. Configuration migration and Spec Kit initialization are separate operations, not part of this transaction.

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

Doctor is read-only: it reports configuration provenance, effective settings, real Bureau interfaces and managed drift. It resolves JSON and the process environment; it does not execute `.env`, authenticate providers, call Linear/models or run tests. Source trusted overrides before invoking it when needed. App mode does not require background CLIs; background GitHub stages require `gh`. The runtime uses the first configured Linear team's state map; doctor warns on multiple teams.

Qualify a representative ticket through the intended app/background path with the review-only boundary before enabling unattended dispatch. Authenticate selected providers normally. Resume workers/automations only when requested and the upgrade is coherent. See the [acceptance record](codex-acceptance.md) for what has and has not been exercised.

## Rollback

Stop new dispatch and inspect active owners before restoring anything. Restore the known previous source revision or published tag in the source clone, then preview a scoped resync and review conflicts. Restore local customizations from their private backups. A configuration backup can be restored after comparing subsequent edits; compare compatibility with the selected runtime before replacing it. Keep `.env` private and intact.

Restoring source/configuration does not undo commits, Linear transitions or already-completed work. Preserve run records, provider logs, issue branches and checkpoints. Older runtimes may not honor new ownership records, so never restart one against unfinished work managed by the newer runtime. Do not delete leases or reset worktrees as a rollback shortcut.
