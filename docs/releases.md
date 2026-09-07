# Releases and upgrade communication

Bureau ships a source skill and templates that are copied into adopting repositories. It has no application package to deploy. A source update and an adopting-repository resync are separate steps, so a release must document both the changes and the required operator actions.

## Current release status

This source tree contains **Bureau v2.0.0**, dated **2026-09-07**, for stable publication. The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v2.0.0) is the publication record and identifies the exact tagged commit. The [changelog](../CHANGELOG.md), [release notes](release-notes.md) and [acceptance record](codex-acceptance.md) describe the changes and available evidence. Source updates still require resyncing each adopting repository.

The official repository is [KaiaK808/bureau](https://github.com/KaiaK808/bureau). Its initial commit used a `v1.0.0` label, but had no corresponding tag or GitHub Release. v2.0.0 is the first versioned release, marking operational changes for worker ownership and legacy upgrades; no v1 release is assigned retroactively. Configuration schema `version: 2` is independent of the Bureau release version, and v1 configurations remain supported.

The qualified integration baseline is [926015031cd95fa058dc159c8f927152fcf6b8f1](https://github.com/KaiaK808/bureau/commit/926015031cd95fa058dc159c8f927152fcf6b8f1). Release preparation changes documentation and the bug-report template only. Verify that runtime, installer, templates and tests remain identical to that baseline, then require full CI on the final release commit. Record that final commit in the GitHub Release body; a committed document cannot contain its own final commit hash.

## Release contract

- Use `vMAJOR.MINOR.PATCH` tags, with an `-rc.N` suffix for release candidates. Before 1.0, put incompatible workflow/configuration changes in a minor release and compatible fixes in a patch release. After 1.0, use a major release for incompatible changes. Document operational changes even if the config schema remains compatible.
- Treat published tags as immutable. Correct a bad release with a new release and an explicit notice; never move the old tag to different source.
- Use [CHANGELOG.md](../CHANGELOG.md) as the canonical user-facing record. Each version gets a date, comparison link, additions/changes/fixes, upgrade actions and known limitations. Keep older untagged history separate instead of inventing past version numbers.
- Create a GitHub Release for the same tag, using the curated changelog entry and a link to the [upgrade guide](migration.md). Generated commit lists can supplement it; they do not replace migration/conflict instructions.
- State the supported/tested environments and evidence level: installer fixtures, real local workflows, live provider smoke, or live adopting-repository ticket. Do not turn passing mocks into a claim of unattended production compatibility.

## Before publishing

1. Choose the release scope and exact commit. Merge its reviewed PRs in dependency order; inspect the final combined diff and verify required CI on that commit. Do not tag an unmerged worktree or a commit with unresolved review findings.
2. Run the repository checks and relevant installation/upgrade cases, including a legacy repo without a manifest, a customized script, user instructions, constitution preservation and config migration/rollback. For runtime releases, include the full Linux/macOS suite and the appropriate live acceptance described in the release notes.
3. Verify the adoption instructions against the selected commit. List each required resync scope, any configuration/provider changes, new dependencies, protected old worktrees and rollback limits. Include what remains opt-in.
4. Move the shipped Unreleased entries to `## [VERSION] - YYYY-MM-DD` in CHANGELOG.md, using the actual version/date, and leave a fresh Unreleased section. Update the release-status paragraphs in this guide and the upgrade guide; record the selected source commit and replace the draft status only when publication is ready.
5. Regenerate the HTML manual with `python3 scripts/render_docs.py` and run `bash tests/test_docs.sh`. Commit the release documentation, then wait for that exact commit's checks. The release tag must include its own notes.
6. Publish an annotated (or signed, when configured) tag on the verified commit and a GitHub Release with the same version. Push only that exact tag, never all local tags. Mark a candidate as a prerelease; mark a stable release as latest. Use tag-bound documentation links and record the final commit in the GitHub Release body. Review the rendered release notes and links before announcing availability.
7. Give existing users the two-step source-update/resync instructions. Record follow-up issues and known limitations; do not silently change released instructions into claims of validation that did not occur.

Example publication commands, **only after** the maintainer has selected a real version and verified commit:

```sh
: "${BUREAU_RELEASE:?Set the selected release tag}"
: "${BUREAU_RELEASE_COMMIT:?Set the verified release commit}"
: "${BUREAU_RELEASE_NOTES:?Set the path to the reviewed release notes file}"
git tag -a "$BUREAU_RELEASE" "$BUREAU_RELEASE_COMMIT" -m "Bureau $BUREAU_RELEASE"
git push origin "refs/tags/$BUREAU_RELEASE"
gh release create "$BUREAU_RELEASE" --verify-tag \
  --title "Bureau $BUREAU_RELEASE" --notes-file "$BUREAU_RELEASE_NOTES" --latest
```

For a candidate, replace `--latest` with `--prerelease --latest=false`. After publishing, verify the remote tag's peeled commit, the release flags and links, and installation from a fresh tag checkout. A tag/release is published only as an explicitly authorized release operation, not as a side effect of writing docs or updating a PR.

## Release-note outline

```markdown
## What changed
- User-visible additions and fixes, with PR links.

## Upgrade from the previous release / legacy untagged install
- Update the source skill to this tag, refresh skill discovery, then run the stated resync scopes in each adopting repo.
- Required dependencies/configuration changes; conflict resolution and backup instructions.
- Changes that are optional, and defaults that affect dispatch or merging.

## Compatibility and rollback
- Supported platforms/providers; legacy configuration behavior.
- How to preserve/restore customizations and unfinished work; external effects that rollback cannot undo.

## Validation and known limitations
- Exact checks and live scenarios exercised on the release commit.
- Remaining limitations and links to the acceptance record or follow-up issues.
```

Bug reports should include the installed source tag/commit (or explicitly say unknown for a legacy copy), selected provider/CLI version and relevant doctor output with credentials removed. A config schema number alone cannot identify the installed runtime release.
