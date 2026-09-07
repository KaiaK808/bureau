# Bureau v2.0.0 — Claude Code and Codex

**Stable release · 2026-09-07 · source tag `v2.0.0`.** The [GitHub Release](https://github.com/KaiaK808/bureau/releases/tag/v2.0.0) records publication and the exact tagged commit. This is the first versioned release; the initial public snapshot's v1.0.0 label did not have a corresponding tag or release. The major version identifies operational changes for existing workers and upgrades, independently of configuration schema numbers.

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

The integration is [PR #9](https://github.com/KaiaK808/bureau/pull/9). See the [changelog](../CHANGELOG.md), [migration guide](migration.md) and [configuration reference](configuration.md). The source repository is [KaiaK808/bureau](https://github.com/KaiaK808/bureau).

## Upgrade an existing installation

Select tag `v2.0.0` in the actual source skill checkout using the [source-update procedure](migration.md#select-the-source-release), refresh skill discovery, and then resync each adopting repository. Updating the source alone, or running `/bureau-init --update`, does not replace installed scripts. Check the source remote, preserve changes and record the old commit before updating; tag-pinned installations do not use `git pull`.

In Claude Code, after updating the source:

```text
/bureau-init --resync-interfaces --resync-scripts --target both
```

In a Codex app task, use:

```text
$bureau-init --resync-interfaces --resync-scripts --target both
```

Use `--target claude` to retain Claude-only interfaces. Refresh Spec Kit separately with `--resync-speckit` and existing planning workflows with `--resync-workflows` where used. Review the preview and each custom-file conflict before applying. CI scaffolding remains opt-in.

Pause new dispatch first. Preserve local and ignored files, credentials, configuration, custom scripts and unfinished worktrees. A legacy install without a manifest must not be treated as permission to overwrite differing files. Old unregistered workers require an explicit inspected handoff before reuse.

V1 configuration remains supported. Normal schema migration preserves generic model fields as Claude settings with `model_compatibility: v1`; select Codex models through provider-specific settings. Adding Codex interfaces does not switch the background provider. One configuration operates one active Linear team.

## Qualification and limits

The [acceptance record](codex-acceptance.md) distinguishes reproducible source fixtures, isolated live provider smoke and a maintainer-reported private adoption. The source suite contains 35 suites covering installation, runtime, providers, recovery, review stops and rendered documentation. Release qualification requires all 35 suites, Bash syntax, ShellCheck and documentation checks on Linux/macOS before tagging. The runtime and installer are unchanged from the live-qualified [integration baseline](https://github.com/KaiaK808/bureau/commit/926015031cd95fa058dc159c8f927152fcf6b8f1); release preparation updates documentation and reporting metadata.

Representative live adoption exercised app stages, bounded Codex and mixed-provider routes, interruption/resume, project tests and durable review stops. It did not enable recurring dispatch or qualify a live automatic merge. Actual project dependencies, network/OS permissions, model access and workload-specific results need qualification in each adopting repository.

App review and bounded ticks stop before merge by default. Continuous background pipelines require explicit stop settings where merging is undesired. Enabling recurring dispatch remains a separate choice.

Rollback restores a coherent installed asset/configuration set after inspecting ownership. It cannot undo already published commits, PRs, Linear transitions or completed work. Published release tags must remain immutable.
