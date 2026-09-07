# Resync

Resolve `BUREAU_INIT_ROOT` from the loaded skill, and use the target git root as `--repo`. Targets default to those recorded in `.bureau-install.json`; older installations default to Claude unless the user selects `--target codex|both`.

| Flag | Helper arguments |
|---|---|
| `--resync-interfaces` | `assets --scope interfaces` |
| `--resync-scripts` | `assets --scope scripts` |
| `--resync-workflows` | `assets --scope workflows` |
| `--resync-ci` | `assets --scope ci` |
| `--resync-speckit` | `speckit` |

Update the source skill before asking it to resync an older installation, and refresh host skill discovery. Updating the clone does not update adopting repositories. For a legacy upgrade or customization conflicts, follow the [upgrade guide](../docs/migration.md); check [release notes](../CHANGELOG.md) for required scopes and remaining limitations.

When multiple asset flags are requested, combine their scopes into one helper preview/apply batch. For example, `/bureau-init --resync-interfaces --resync-scripts --target both` maps to `assets --target both --scope interfaces --scope scripts`. Spec Kit is a separate operation; CI and workflows are included only when requested. Pause dispatch and preserve active/unfinished work before changing scripts.

Example: `python3 "$BUREAU_INIT_ROOT/scripts/bureau_install.py" assets --repo "$PWD" --scope scripts`.

Preview first, read changed files and then repeat with `--apply` for authorized changes. Compare each conflict with its source under the installed skill. Unchanged files are adopted without a prompt; previously installed unmodified files can update automatically. Customized files or files from an installation without hashes report conflicts. To replace one after review, append `--overwrite scripts/FILE.sh` (repeat for multiple paths). If one conflict remains, the entire asset batch writes nothing. To retain custom behavior while installing a complete batch, first back up conflicting originals privately and review the intended merge. Apply the coherent upstream batch with each authorized replacement named, then selectively reapply the reviewed custom changes before resuming dispatch. Those files will intentionally conflict on the next preview; report that drift instead of editing the manifest to hide it. If a resolution is deferred, leave the batch unapplied. The helper has no skip-conflicts or automatic three-way merge mode. Never set blanket overwrite flags.

Instruction files manage only `<!-- bureau-init:begin -->` through `<!-- bureau-init:end -->`. Text outside the block is preserved. For the old `<!-- bureau-init managed -->` format, first identify the exact generated section and delimit it with the new markers without including user guidance; review it before adoption. Do not replace the whole file to resolve a block conflict.

Spec Kit resync uses the pinned CLI (0.7.5). Bureau uses its drift manifests to snapshot and restore customized skills/templates; untracked existing assets are preserved conservatively. Both installed integrations are refreshed; the previous active integration stays active unless `--active-integration` requests a switch. Existing constitution bytes are restored even if initialization fails. On failure, active metadata is restored but partial new assets may remain: inspect before retrying. Do not silently reinstall a different CLI version.

When Codex is selected and the Git extension is already installed, resync also renders its native Codex skills in a temporary directory using the pinned CLI. It installs only missing skills and merges Codex command registrations after the files exist. Existing skills, hook settings, extension configuration, metadata and other host registrations are preserved. Preview reports this separate registration step; a renderer failure occurs before core initialization. This repairs legacy installations whose core resync alone omitted mandatory Git hooks.

Resolve the exact approved spec directory when using helpers from a `codex/*` or detached checkout. Pass both `SPECIFY_FEATURE=NNN-approved-feature` and `SPECIFY_FEATURE_DIRECTORY=specs/NNN-approved-feature` to each helper invocation, using the real configured path. Older retained helpers use the former; newer helpers can prefer stale feature.json unless the latter is explicit. Do not infer a feature from a duplicate numeric prefix or rename an issue branch to satisfy a helper.

CI is opt-in and remains on `runs-on: ubuntu-latest`; preserve an existing workflow unless replacement is authorized. Do not switch public fork PR workflows to self-hosted runners (fork-PR-RCE).

Resync does not modify `.bureau.json` or `.env`, and each scope excludes unrelated assets. Re-run the preview to verify the final state and report retained local differences.
