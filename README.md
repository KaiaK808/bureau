# Bureau

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Bureau connects Linear tickets, Spec Kit artifacts, implementation, QA and review. Use it directly in a Codex app task, through Claude Code commands, or in a background pipeline with Claude, Codex, or a mixture of providers. State lives in Linear and Git; there is no application backend to host.

![Bureau archive chamber](docs/assets/hero.png)

## Choose how to work

| Mode | Where creative work runs | Entry point |
|---|---|---|
| Codex app task | Current task, using its selected model and tools | `$bureau TEAM-123 through review` |
| Claude interactive | Current Claude Code session | `/linear-to-spec TEAM-123`, `/linear-implement TEAM-123` |
| Bounded background tick | Configured provider CLI in a claimed disposable worktree | `bash scripts/bureau-tick.sh --no-merge` |
| Continuous background pipeline | Configured provider CLIs, supervised shell loops/tmux | `bash scripts/start-bureau-v2.sh` |

App work preserves the current checkout. Background workers claim both the ticket and their own disposable checkout. Held branches and unfinished work are preserved for reconciliation. App review and bounded ticks stop before merge by default; continuous legacy pipelines can merge after approval and the full CI/base gate. Use `--no-merge` when supervising through review only.

Codex support is implemented and covered by local fixtures. Before unattended adoption, qualify a representative live ticket in your repository. See the [acceptance record](docs/codex-acceptance.md) for verified coverage and remaining live checks.

## Install

Install the stable v3.2.1 source, keep one clone and expose it to the assistants you use:

```sh
git clone --branch v3.2.1 --single-branch https://github.com/KaiaK808/bureau.git "$HOME/bureau-init"
mkdir -p "$HOME/.agents/skills"
ln -s "$HOME/bureau-init" "$HOME/.agents/skills/bureau-init"
# Optional Claude Code entry point:
mkdir -p "$HOME/.claude/skills"
ln -s "$HOME/bureau-init" "$HOME/.claude/skills/bureau-init"
```

Keep existing installations and links; do not overwrite them. This clone is pinned to the release in detached-HEAD state; future upgrades select another published tag as described in the [upgrade guide](docs/migration.md). In an adopting Git repository, invoke `$bureau-init --target codex`, `/bureau-init --target claude`, or select `--target both`. For both, choose which Spec Kit integration should remain active. Refresh skill discovery after installation.

The installer discovers Linear settings, writes `.bureau.json`, initializes pinned Spec Kit 0.7.5, and installs only the selected interfaces. Codex uses `.agents/skills` and AGENTS.md; Claude uses `.claude/commands`, `.claude/skills` and CLAUDE.md. Existing project instructions and the constitution survive resync. Hashes in `.bureau-install.json` identify customizations before updates, and the manifest records which source revision (tag and commit) each asset scope was installed from.

First setup checks prerequisites, selects the host and operating mode, discovers one active Linear team and its projects/labels/states, collects agent choices, writes configuration, installs assets and validates the result. Existing choices are reused. Environment examples preserve credentials, and CI scaffolding is optional with `ubuntu-latest` as its default. See the [conditional setup procedure](references/setup.md).

Installed assets include `.specify/` and the selected hosts' skills/instructions, shared runtime files under `scripts/`, optional Claude planning workflows under `.claude/workflows/`, and optional `.github/workflows/ci.yml`. `specs/` contains feature artifacts; `LESSONS.md` can hold curated learnings. Local configuration, installer bookkeeping and runtime logs remain ignored. Spec Kit comes from the pinned CLI; it is not vendored in this repository.

| Dependency | Needed for |
|---|---|
| Python 3.9+, Bash 3.2+, Git 2.26+, jq, curl | Installation and shared runtime |
| `specify-cli` 0.7.5 | Spec Kit initialization/resync |
| Linear account and `LINEAR_API_KEY` in ignored `.env` | Shared ticket reads/transitions; interactive tools can assist discovery |
| GitHub CLI (`gh`) | Background PR creation and merge operations |
| Claude CLI and/or Codex CLI | Only providers selected for background stages; not required to run a current Codex app task |
| tmux | Persistent pane launchers; not required for app tasks or one-shot ticks |
| Node.js 18+ | Shared conflict-schedule CLI |

On macOS, `brew install jq gh tmux` supplies common background dependencies. Authenticate each selected provider and `gh` normally. Never put credentials in tracked files. The shell helpers use Linear's GraphQL API directly.

## Work in a Codex app task

After installation, use `$bureau spec TEAM-123`, `$bureau implement TEAM-123`, `$bureau qa TEAM-123`, `$bureau review TEAM-123`, or `$bureau TEAM-123 through review`. These work in the current task without launching a nested model. `$bureau status` inspects ownership and saved runs; `$bureau resume RUN_ID` continues preserved work.

The [app setup guide](templates/skills/bureau/references/app-setup.md) gives setup/status/test actions to add through app settings. `repo.test_command` must name the project's real tests. The app generates its own local-environment configuration; Bureau does not overwrite unrelated `.codex` settings.

The [stage protocol](docs/stage-protocol.md) checks configured state IDs, ownership, current HEAD, canonical branch and completion evidence before posting a digest or advancing a ticket. A review request stops at review. Explicitly authorized merging still uses the existing complete merge gate.

## Background execution

Set `agents.runner` to `claude` (legacy default) or `codex`; stage objects can select another runner. Per-provider models avoid leaking a Claude model into a Codex stage. See [provider configuration](docs/provider-runtime.md).

```sh
# Read-only checks, then one bounded stage:
python3 scripts/bureau-doctor.py --mode background
bash scripts/bureau-tick.sh --no-merge
# One named ticket, preserving the review boundary:
bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123
# Pause future dispatch; existing work finishes normally:
python3 scripts/bureau-runtime.py pause
python3 scripts/bureau-runtime.py unpause
```

`logs/bureau-tick.json` distinguishes waiting, advancement, actual Done, review boundaries and failures. Provider evidence is under `logs/provider-runs/`. Missing account usage or dollar cost is unavailable, not zero. The [supervision guide](templates/skills/bureau/references/supervision.md) covers quiet change notifications, native app scheduling when requested, and shared conflict scheduling. Installation does not start recurring automation.

The existing single-ticket, batch and upstream-port modes remain available:

| Operation | Entry point |
|---|---|
| One ticket through review | `bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123` |
| Preview a batch schedule | `bash scripts/orchestrate.sh --execute --schedule schedule.json --max-concurrent 3 --no-merge --dry-run` |
| Execute that reviewed schedule | Repeat without `--dry-run`; a review stop halts its serial lane |
| Port a selected upstream change | `bash scripts/upstream-port.sh --sha COMMIT` or `--pr NUMBER`, after configuring that repository's upstream and build/test commands |

The [background operations reference](references/operations.md) covers these modes, the installed driver/helper inventory, single-flight limits, multiple repositories and the scope of dry-run controls. `agents.max_concurrent_issues: 1` prevents Spec from admitting another issue while work is in flight; it does not replace ownership checks or schedule validation.

## Update and migrate

Select `v3.2.1` in the source clone that supplies `bureau-init` using the [source-update instructions](docs/migration.md#select-the-source-release) and refresh skill discovery. From v3.2.0, a resync is optional because the scripts and interfaces change only in comments, help texts and example values; installers will report those files as updates. From v3.1.0 or older, resync **each adopting repository** following [Upgrade to v3.2](docs/migration.md#upgrade-to-v32) and the earlier notes for your version. In Claude Code, after loading the new source skill:

```text
/bureau-init --resync-interfaces --resync-scripts --target both
```

In a Codex app task, request the same scopes with:

```text
$bureau-init --resync-interfaces --resync-scripts --target both
```

Use `--target claude` to stay Claude-only. Refresh Spec Kit separately with `--resync-speckit --target both`; the existing active integration stays active unless you request a switch. Repos with installed Claude planning workflows also need `--resync-workflows`; script resync does not update them. `--update` is configuration-only. Updating the source clone alone does not change installed project files.

Pause dispatch and preserve local work first. Resync previews conflicts; one unresolved conflict blocks the entire asset batch. The [upgrade and conflict guide](docs/migration.md) covers legacy installations without hashes, named replacements, retaining custom behavior, validation and rollback. Do not use a blanket overwrite or replace project instruction files wholesale.

Version 1 configs remain supported. Optional schema-v2 migration preserves existing values, adds the absent legacy runner default and records `model_compatibility: "v1"` to retain model ownership. Its private backup covers configuration only. Installing Codex interfaces does not switch the background runner.

The [v3.2.1 release](https://github.com/KaiaK808/bureau/releases/tag/v3.2.1) and [changelog](CHANGELOG.md) list what changes and what adopters must do. v2.0.0 was the first tagged release; earlier source installs were untagged. The [release process](docs/releases.md) defines immutable tags, GitHub Release notes and qualification before publication. Bureau release numbers are independent of configuration schema versions.

v3 changes the exit-code contract between stages and their drivers and needs configuration set before the resync; see the [v3.0.0 release notes](docs/release-notes-v3.0.0.md) and [Upgrade to v3](docs/migration.md#upgrade-to-v3). v3.0.1 is a patch on v3.0.0 (the merge stage's exit codes and a reused approval; see its [release notes](docs/release-notes-v3.0.1.md)); v3.0.2 is a patch on v3.0.1 (every stage finds the ticket's own spec directory, and `repo.worktree_links` brings a Python `.venv` back into the stage worktrees; see its [release notes](docs/release-notes-v3.0.2.md)). The previous major release is [v2.0.0](https://github.com/KaiaK808/bureau/releases/tag/v2.0.0).

v3.1.0 is a minor release on v3.0.2 and keeps the v3 exit-code contract; branch code runs without the Bureau secrets, the review stage acts on the result of its inline merge, the merge gate honours `false` switches, pins the head it checked and reads its numbers by one rule, the default timeout per stage call is one hour, and an interrupted run prints its own way back. Resync the scripts and interfaces scopes together and check the list in [Upgrade to v3.1](docs/migration.md#upgrade-to-v31) first; see its [release notes](docs/release-notes-v3.1.0.md) and those of its candidates, [v3.1.0-rc.2](docs/release-notes-v3.1.0-rc.2.md) and [v3.1.0-rc.1](docs/release-notes-v3.1.0-rc.1.md). v3.1.0 followed [v3.0.2](https://github.com/KaiaK808/bureau/releases/tag/v3.0.2).

v3.2.0 is a minor release on v3.1.0 and keeps the v3 exit-code contract; stages keep the `.env` keys unexported, remote git runs without branch hooks, implement retries timeouts and pushes failed work, rework receives the newest review or QA findings, queues move past waiting gates, and a hang-up stops the run. An upgrade from v3.1.0 resyncs the scripts and interfaces scopes together, restarts queue loops and supervisors, and follows [Upgrade to v3.2](docs/migration.md#upgrade-to-v32); Git LFS repositories need `repo.remote_git_runs_hooks: true` before resync. See the [v3.2.0 release notes](docs/release-notes-v3.2.0.md) and those of its candidate, [v3.2.0-rc.1](docs/release-notes-v3.2.0-rc.1.md).

v3.2.1 is the current stable release, marked as latest, a patch on v3.2.0 that removes internal references without behaviour change; see its [release notes](docs/release-notes-v3.2.1.md). From v3.2.0 or v3.2.0-rc.1, select `v3.2.1`; a resync is optional to receive the cleaned comments and help texts (installers report those files as updates). The previous stable release is [v3.2.0](https://github.com/KaiaK808/bureau/releases/tag/v3.2.0).

A release candidate, [v3.3.0-rc.1](https://github.com/KaiaK808/bureau/releases/tag/v3.3.0-rc.1), dated 2026-10-07, is published as a prerelease for piloting v3.3.0. It keeps the v3 exit-code contract and contains one pull request ([#60](https://github.com/KaiaK808/bureau/pull/60)): a shell gate for Codex implement turns blocked only by tests the sandbox denies, and a private temporary directory for Codex calls without `TMPDIR`; Claude runs are unchanged. From v3.2.1 resync the scripts scope as one set, keep `repo.test_command` configured with the full project gate, restart queue loops and supervisors, and follow [Upgrade to v3.3](docs/migration.md#upgrade-to-v33). See its [release notes](docs/release-notes.md). v3.2.1 stays the stable release marked as latest until v3.3.0 is published.

## Documentation

| Reference | Contents |
|---|---|
| [Configuration](docs/configuration.md) | State maps, stage toggles, provider settings and overrides |
| [Provider runtime](docs/provider-runtime.md) | Auth, permissions, structured results, timeouts and evidence |
| [Operator cheat sheet](docs/OPERATOR-CHEATSHEET.md) | App tasks, one-shot runs and batch scheduling |
| [Exit codes](docs/exit-codes.md) | Failure, quota, ownership and stop outcomes |
| [Recipes](docs/recipes.md) | Single-flight, provider mixing and dry-run examples |
| [Troubleshooting](docs/troubleshooting.md) | Diagnostics, recovery and permissions |
| [Token efficiency](docs/token-efficiency.md) | Optional Claude capabilities and their limits |
| [Upgrade guide](docs/migration.md) | Source update, per-repository resync, conflicts and rollback |
| [Releases](docs/releases.md) | Release status, versioning and publication checklist |
| [Acceptance record](docs/codex-acceptance.md) | Automated coverage and live adoption checks |

The same documents are [available as styled HTML](docs/site/index.html). Contributions follow [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Run `bash tests/run.sh` for the local test harness.

For contributors, `SKILL.md` routes into conditional `references/`; `scripts/bureau_install.py` owns installation. `templates/scripts/` is the runtime source, `templates/commands/` supplies Claude commands and rendered Codex skills, `templates/instructions/` supplies managed instructions, and `templates/workflows/` supplies optional planning workflows. Runtime templates win when prose disagrees.

Built by [Kai Ebert](https://github.com/KaiaK808) / Brainhuggers under the [MIT license](LICENSE). See [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and [NOTICE](NOTICE). Bureau can write code and change Linear/GitHub state; select its operating scope for the repository you intend to use.
