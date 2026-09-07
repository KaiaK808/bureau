# Contributing to Bureau

Thanks for the interest. Bureau is small and opinionated, so contributions land fastest when they're aligned with the design already in the tree. Read this before you invest time.

## Try it first

Follow the [installation guide](README.md#install) for Claude, Codex, or both. Test in a throwaway/adopting repository and keep live ticket/model validation separate from fixture coverage.

## Report a bug

Open an [issue](https://github.com/KaiaK808/bureau/issues/new/choose) with the bug-report template. The template asks for the specific things I need to reproduce: selected provider version, OS, the exact command you ran, and what you expected vs. what happened. Bug reports without those details will be closed with a request for them.

Do NOT file public issues for security vulnerabilities — see [SECURITY.md](SECURITY.md).

## Send a PR

- **Branch from `main`.** No forks-of-forks tangles.
- **Run the tests before you push.** `bash tests/run.sh` must pass (the CI job runs exactly this). See `AGENTS.md` for the harness structure.
- **One focused change per PR.** Rebrand + genericize + hero image was three PRs, not one. This is the norm.
- **PR body follows the existing recent pattern** — Summary, What changed, Test plan, After merge. Look at any of the merged PRs for the shape.
- **No new dependencies without a plan.** The shared runtime uses Bash, stdlib Python and a small JavaScript scheduler; keep dependencies explicit and minimal.
- **Release notes travel with user-facing changes.** Add an entry under `Unreleased` in [CHANGELOG.md](CHANGELOG.md), including adoption actions, compatibility/default changes and relevant limitations. Update [the upgrade guide](docs/migration.md) when existing installs need a different command or conflict procedure. Do not invent a released version or mark pending work as shipped.
- **Docs travel with code.** If you add a `.bureau.json` flag, it lands in `docs/configuration.md` in the same PR. If you add a script, it gets a header comment. This is enforced by review.

## Releases

Maintainers follow [the release process](docs/releases.md): select a reviewed commit, qualify it, prepare dated notes and upgrade instructions, then publish an immutable tag and matching GitHub Release. Configuration schema versions are independent of release tags. A documentation PR does not itself publish a release.

## Development notes

The heart of the project is in `templates/scripts/` (the shell scripts that get installed into target repos) and `SKILL.md` (the portable installer skill followed by Claude Code and Codex). Everything else is docs, tests, or scaffolding.

`AGENTS.md` is the maintainer's reference — read it before touching the pipeline scripts. It documents the invariants (never spawn `$CLAUDE` for Linear CRUD, branches resolved via the `bureau-branch` marker, exit codes are a protocol) that keep the system from silently wedging in cron.

## What's out of scope

- **Rewriting bash to another language.** Bash is a constraint, not a bug. If you want a Rust rewrite, fork it.
- **Making Bureau backend-agnostic.** Linear is the source of truth. A Jira-backed variant is a fork, not a PR.
- **Adding a hosted application backend.** Bureau uses the existing app/terminal surfaces and local scripts.

## Communication

The project doesn't have a Slack / Discord / mailing list. Issues and PRs on GitHub are the communication surface. Keep it there.
