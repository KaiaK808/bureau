# Claude/Codex support — draft release notes

**Unreleased.** No version, tag or GitHub Release has been selected or published. These notes describe the candidate source changes; the final release requires the verified commit and version described in the [release process](releases.md).

Bureau can install interfaces for Claude Code, Codex, or both. Codex app tasks can prepare, perform, validate and resume individual Linear stages in their current checkout. Background stages can select Claude or Codex independently of the app model.

## Changes

- Native Codex skills and managed AGENTS.md guidance, preserving Claude interfaces and project instructions.
- Deterministic installation previews and manifests, per-file conflict resolution, legacy Spec Kit integration and Git-hook preservation.
- Shared issue/workspace ownership, protected user checkouts and interrupted-work recovery.
- Provider adapters with bounded execution and structured results, provider-specific models/usage, diagnostics and bounded supervision.
- Review stops that persist across repeated ticks and environment loading, including configurations without a separate merge stage.
- Provider timeout/cancellation cleanup that stops descendants even after the immediate CLI process exits.
- Reviews of dependent PRs use their actual target branch and pinned commits; retargeting or changing those commits prevents stale approval.
- Configuration migration, Linux/macOS checks, and upgrade/rollback documentation.
- Updated field manual retaining the official repository's configuration, recipe and troubleshooting coverage.

See the [changelog](../CHANGELOG.md), [migration guide](migration.md) and [configuration reference](configuration.md). The source repository is [KaiaK808/bureau](https://github.com/KaiaK808/bureau).

## Upgrade an existing installation

Update the actual source skill checkout to the reviewed `main` commit or a published release tag, refresh skill discovery, and then resync each adopting repository. Updating the source alone, or running `/bureau-init --update`, does not replace installed scripts. Check the source remote before updating.

In Claude Code, after updating the source:

```text
/bureau-init --resync-interfaces --resync-scripts --target both
```

Use `--target claude` to retain Claude-only interfaces. Refresh Spec Kit separately with `--resync-speckit` and existing planning workflows with `--resync-workflows` where used. Review the preview and each custom-file conflict before applying. CI scaffolding remains opt-in.

Pause new dispatch first. Preserve local and ignored files, credentials, configuration, custom scripts and unfinished worktrees. A legacy install without a manifest must not be treated as permission to overwrite differing files. Old unregistered workers require an explicit inspected handoff before reuse.

V1 configuration remains supported. Normal schema migration preserves generic model fields as Claude settings with `model_compatibility: v1`; select Codex models through provider-specific settings. Adding Codex interfaces does not switch the background provider. One configuration operates one active Linear team.

## Qualification and limits

The [acceptance record](codex-acceptance.md) distinguishes reproducible source fixtures, isolated live provider smoke and a maintainer-reported private adoption. The source suite contains 35 suites covering installation, runtime, providers, recovery, review stops and rendered documentation. The selected release commit must pass its own full Linux/macOS CI before tagging.

Representative live adoption exercised app stages, bounded Codex and mixed-provider routes, interruption/resume, project tests and durable review stops. It did not enable recurring dispatch or qualify a live automatic merge. Actual project dependencies, network/OS permissions, model access and workload-specific results need qualification in each adopting repository.

App review and bounded ticks stop before merge by default. Continuous background pipelines require explicit stop settings where merging is undesired. Enabling recurring dispatch remains a separate choice.

Rollback restores a coherent installed asset/configuration set after inspecting ownership. It cannot undo already published commits, PRs, Linear transitions or completed work. Published release tags must remain immutable.
