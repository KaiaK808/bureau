# Changelog

User-visible changes and upgrade actions are recorded here. Releases use dated version sections and immutable Git tags; see the [release process](docs/releases.md). Earlier changes on `main` were not tagged and remain listed separately below. Configuration schema numbers are not Bureau release versions.

An upgrade requires **updating the source skill and resyncing each adopting repository**. Neither `git pull` alone nor `/bureau-init --update` refreshes installed assets. Follow the [upgrade and conflict guide](docs/migration.md).

## [Unreleased]

## [2.0.0] - 2026-09-07

### Claude Code and Codex support

Bureau v2.0.0 includes the Claude/Codex integration and its review fixes ([#9](https://github.com/KaiaK808/bureau/pull/9)). It is the first versioned release; the public initial snapshot was labeled v1.0.0 but had no corresponding tag or GitHub Release. The major version marks the operational changes for existing workers and upgrades. Installing a source update still requires a target-repository resync. See the [v2.0.0 release notes](docs/release-notes.md).

#### Added

- Claude/Codex/both installation targets, native Codex skills and AGENTS.md, plus a `$bureau` operator that works in the current app task. Existing Claude commands remain available.
- Deterministic asset previews, installation hashes, per-file conflict handling and preservation of project instructions, active Spec Kit integration and constitution.
- Shared Claude/Codex provider adapters, bounded background ticks, quiet supervision, read-only diagnostics and additive config migration with an exact private backup.
- An upgrade/conflict guide, an explicit source-update versus target-resync procedure, and a release process for dated notes, immutable tags and GitHub Releases.
- Linux/macOS qualification and an acceptance record that distinguishes fixtures, live provider smoke and the limits of live adopting-ticket validation.

#### Changed

- Background workers require explicit registration and issue/workspace ownership. Old unregistered worktrees and app-owned checkouts refuse automatic reset. Interrupted runs retain ownership for inspected recovery.
- App review and bounded ticks stop before merge by default. Continuous pipelines retain gated merging; use `--no-merge` when that boundary is required.
- Legacy generic model settings retain their Claude meaning. V1-to-v2 config migration records `model_compatibility: v1`; opting into v2 generic model semantics is a separate choice. Installing Codex interfaces does not switch the background provider.
- Completed review stops persist across ticks. New ticket/remote inputs or explicit resume reopen review; preserved clean review checkpoints can be retained while another verified worker proceeds.

#### Fixed

- Background code review uses the pull request's actual target branch, including dependent PRs. It pins the fetched base and head for review; changing the target or either remote commit invalidates a saved review stop. Missing or invalid target metadata stops review before provider calls.

- Provider timeout and cancellation now stop descendants that ignore termination after the immediate CLI process exits. Cleanup covers the whole process group before the bounded invocation returns, preserving timeout/cancellation exit codes.

- Setup now selects one active Linear team, matching runtime support. Model-update guidance preserves v1 Claude defaults when configuring Codex and verifies effective provider settings. Historical analysis links point to the reviewed source baseline; public qualification claims stay within the documented evidence.

- Explicit review-only requests remain enforced when a pipeline loads an older environment file. Review and merge gates retain the captured caller boundary even if that file clears user-facing stop flags; an ordinary run without a stop request keeps its existing behavior.

- Adding Codex to a legacy installation now registers an existing Git extension's native hook skills through the pinned Spec Kit CLI, preserving customized skills and hook settings. Refreshed instructions map dotted hook names and explicitly select feature directories across retained and current helper versions.

- Symlinked worker paths (including macOS `/tmp`) resolve to their physical checkout before branch-owner comparison, preventing a worker from falsely conflicting with itself while retaining protection for other checkouts.

- Surviving nested workers can no longer release a checkout for an unsafe automatic reset. Human blockers remain visible to scheduling and notifications.
- App finish retries cannot publish contradictory results; routing uses current labels and background implementation consumes app review feedback.
- Newly created QA/spec-review/copy files are published, rejected pushes prevent advancement, and staged private files refuse publication without losing staged work.
- Legacy Spec Kit active integrations survive adding a second host. Diagnostics reject generic instruction files as installation evidence and detect missing GitHub dependencies.

#### Upgrade actions and limitations

- Select source tag `v2.0.0` first, then use `/bureau-init --resync-interfaces --resync-scripts --target both` in each adopting repo (or `--target claude` to stay Claude-only). In Codex, use `$bureau-init` with the same arguments. Refresh Spec Kit separately with `--resync-speckit`; refresh installed planning workflows with `--resync-workflows` if used. CI scaffolding stays opt-in.
- Pause dispatch, preserve local/ignored files and reconcile conflicts before starting the new runtime. Differing files without a prior manifest are conflicts, not disposable generated output. See the [per-file resolution procedure](docs/migration.md#preview-and-resolve-asset-conflicts).
- Configuration migration is optional while v1 is supported. Review dependencies, model ownership, old worker checkouts and rollback limits in the [upgrade guide](docs/migration.md).
- A representative live adoption exercised app stages, a bounded Codex route, a mixed-provider route, interruption/resume, real project tests and durable review stops. This is not a qualification of arbitrary projects or recurring unattended dispatch; private evidence is summarized separately from reproducible source checks in the [acceptance record](docs/codex-acceptance.md).

## Earlier changes on main (untagged)

The following history predates versioned releases. It does not assign release numbers or dates retrospectively. Earlier implementation work was included in the [public initial snapshot](https://github.com/KaiaK808/bureau/commit/6763c26c26aa96a41a92fbe95416fddbf4d48f69); PR numbers from its previous repository are not part of this public repository's PR history.

### Public documentation and CI updates

- Expanded the README, configuration reference, recipes, troubleshooting and operator guidance ([#2](https://github.com/KaiaK808/bureau/pull/2), [#3](https://github.com/KaiaK808/bureau/pull/3), [#4](https://github.com/KaiaK808/bureau/pull/4), [#5](https://github.com/KaiaK808/bureau/pull/5), [#6](https://github.com/KaiaK808/bureau/pull/6)). The integration retains this coverage and updates it for the current runtime.
- Updated the pinned checkout action and its maintenance comment ([#1](https://github.com/KaiaK808/bureau/pull/1), [#7](https://github.com/KaiaK808/bureau/pull/7)).

### Open-source launch preparation

- **CI runner moved off self-hosted** — GitHub-hosted `ubuntu-latest` is now the default (was `[self-hosted, linux, ARM64]` on a production Hetzner box). Removes the fork-PR-RCE class for public-repo PR CI. Least-privilege `GITHUB_TOKEN` scope (`contents: read`) added top-level.
- **Proprietary Atipo Foundry fonts removed** — the six Babcock + Silka `.woff2` files that shipped in `docs/site/assets/fonts/` had a non-redistributable EULA; the MIT license couldn't cover them. Working tree cleaned; CSS falls through to a system-sans stack. Downstream sites with a valid Atipo license can layer their own `@font-face` blocks to restore the display faces transparently.
- **Quick fixes** — clone remote updated to `KaiaK808/bureau`, `.gitignore` gets `.env` / `.pem` / `.log` rules, internal infra references genericized in committed public files, template default flipped to `ubuntu-latest`.
- **Supply-chain hygiene** — `actions/checkout` pinned to full SHA, `.github/dependabot.yml` added for the `github-actions` ecosystem so pinned actions get bumped automatically.
- **Community + discoverability** — `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1), `.env.example`, `.github/ISSUE_TEMPLATE/config.yml`, `NOTICE` documenting third-party asset provenance.
- **Open-source hygiene** — `CONTRIBUTING.md`, `SECURITY.md`, issue + PR templates, README badges, MIT LICENSE.
- **Rebrand** — project renamed from "bureau-init" to "Bureau" in prose; repo renamed on GitHub; slash command `/bureau-init` unchanged. On-brand hero image added (`docs/assets/hero.png` — a dim ceremonial archive chamber, wall of drawers, one glowing hot pink).

### Token efficiency

- **`/goal` + caveman + Headroom** — three opt-in compression layers in `.bureau.json` `agents.*`. `/goal` replaces the implement-pipeline retry loop with Claude Code's native slash command (Haiku evaluates per turn); caveman compresses review-prose output ~65%; Headroom wraps the `claude` binary for 60–95% input-token reduction on tool-output-heavy stages. Composes for ~80% fewer tokens per pipeline tick.

### Pipeline & upstream-port fixes

- Codex code-review no longer captures stderr (fixed `ARG_MAX` overflow on merge), severity floor for security findings, caveman wiring in review prose, headroom `wrap claude --` separator
- Upstream-port `--with-llm` flag — Claude-assisted conflict resolution with cost gate
- Upstream-port `.bureau-port-map.json` for path renames + dropped files
- Upstream-port script itself, introduced for downstream repositories
- Implement-pipeline: match tasks.md by leading number, ready-flip on PARTIAL+commits, stuck-detector skip on COMPLETE, PARTIAL+prior-commits doesn't force STUCK

### Infrastructure

- Self-hosted CI runner migration (later reverted for public-repo safety)
- Model-resolution precedence fix — env > per-stage JSON > workspace JSON > env default, live-read
- Baseline test-suite fixes

For changes since the public initial snapshot, `git log --oneline main` is authoritative.

[Unreleased]: https://github.com/KaiaK808/bureau/compare/v2.0.0...main
[2.0.0]: https://github.com/KaiaK808/bureau/compare/6763c26c26aa96a41a92fbe95416fddbf4d48f69...v2.0.0
