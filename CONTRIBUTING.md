# Contributing to Bureau

Thanks for the interest. Bureau is small and opinionated, so contributions land fastest when they're aligned with the design already in the tree. Read this before you invest time.

## Try it first

Follow the [installation guide](README.md#install) for Claude, Codex, or both. Test in a throwaway/adopting repository and keep live ticket/model validation separate from fixture coverage.

## Report a bug

Open an [issue](https://github.com/KaiaK808/bureau/issues/new/choose) with the bug-report template. The template asks for the specific things I need to reproduce: selected provider version, OS, the exact command you ran, and what you expected vs. what happened. Bug reports without those details will be closed with a request for them.

Do NOT file public issues for security vulnerabilities — see [SECURITY.md](SECURITY.md).

## Send a PR

- **Branch from `main`.** No forks-of-forks tangles.
- **Run the tests before you push.** `bash tests/run.sh` must pass. See `AGENTS.md` for the harness structure and the CI coverage below.
- **One focused change per PR.** Rebrand + genericize + hero image was three PRs, not one. This is the norm.
- **PR body follows the existing recent pattern** — Summary, What changed, Test plan, After merge. Look at any of the merged PRs for the shape.
- **No new dependencies without a plan.** The shared runtime uses Bash, stdlib Python and a small JavaScript scheduler; keep dependencies explicit and minimal.
- **Release notes travel with user-facing changes.** Add one `changes/unreleased/<slug>.md` fragment following [the fragment format](changes/README.md), including adoption actions, compatibility/default changes and relevant limitations; run `python3 scripts/changelog.py check`. Do not add entries directly under `Unreleased` in [CHANGELOG.md](CHANGELOG.md). Update [the upgrade guide](docs/migration.md) when existing installs need a different command or conflict procedure. Do not invent a released version or mark pending work as shipped.
- **Docs travel with code.** If you add a `.bureau.json` flag, it lands in `docs/configuration.md` in the same PR. If you add a script, it gets a header comment. This is enforced by review.

## CI coverage

Every pull request runs the complete Linux suite in four shards and the focused
macOS manifest in two shards. Pushes to `main`, `release/**` branches and `v*`
tags, the weekly Monday schedule and manual workflow dispatch run the complete
macOS suite in six shards. Check names distinguish `focused` and `full` runs;
every job uploads its timing records, including on failure.

Add a new or changed test to [the macOS manifest](tests/macos-manifest.txt) when
it depends on Bash 3.2, BSD sed/date/stat/mktemp/grep, installation/resync or path
handling (including symlinks/worktree links), permissions/execute bits, process
groups/signals/kill/reaping, PTYs/native process discovery (ps/lsof/pgrep fallbacks)
or sandbox profiles, or guards a macOS-only regression. Give each entry a short
reason based on the assertions it exercises. Incidental fixture cleanup alone
does not make a general integration test a platform test.

Run the focused selection locally with
`BUREAU_TEST_MANIFEST=tests/macos-manifest.txt bash tests/run.sh`; append `--list`
to inspect it. Manifest paths are relative to the invoking directory. Blank lines
and comments are ignored; unknown entries or an empty manifest fail before any
test runs. Filtering happens before optional `BUREAU_TEST_SHARD=i/n` balancing.
Refresh weights from complete successful timing artifacts and recalculate the
workflow's estimate when changing the manifest. The 56-entry manifest currently
estimates 89.98 macOS runner-minutes, including four minutes of setup across two
jobs; hosted queue time is separate.

## Releases

Maintainers follow [the release process](docs/releases.md): select a reviewed commit, qualify it, prepare dated notes and upgrade instructions, then publish an immutable tag and matching GitHub Release. Configuration schema versions are independent of release tags. A documentation PR does not itself publish a release.

## Development notes

The heart of the project is in `templates/scripts/` (the shell scripts that get installed into target repos) and `SKILL.md` (the portable installer skill followed by Claude Code and Codex). Everything else is docs, tests, or scaffolding.

`AGENTS.md` is the maintainer's reference — read it before touching the pipeline scripts. It documents the invariants (never spawn `$CLAUDE` for Linear CRUD, branches resolved via the `bureau-branch` marker, exit codes are a protocol) that keep the system from silently wedging in cron.

## Weekly baseline

From this source checkout, run `python3 -I scripts/bureau-baseline.py --repo PATH --weeks 4 --format md` (or `--format json`). This maintainer tool is not installed into adopting repositories. It prints only to stdout and reads local Git metadata, merged PRs through authenticated `gh`, and current Linear snapshots through the trusted template's `bureau_wait_snapshot` when configuration and credentials are available. It never invokes stages, fetches Git objects, writes evidence or changes Linear. Do not commit reports or target-repository data.

Weeks are UTC ISO weeks including the partial current week. Completion uses each known ticket's first known PR merge, otherwise its dated Linear Done snapshot; review rounds and estimated USD costs use the merged cohort, and defects use the fix's merge week. Sources under `<git common dir>/bureau/evidence/v1/` are `stage-outcomes.jsonl`, `review-dispositions.jsonl`, `runs/*/manifest.json`, `runs/*/receipts/*.json` and `tickets/*.json` (`bureau-evidence.py`). The external cost log is read through `BUREAU_COST_DIR` (`bureau-config.sh:record_stage_cost`). Worktrees share the store. Evidence is prunable, so these are retained observations, not a census. Each metric carries sources, quality and a reason. Historical eligibility and Triage entry stay missing; spec starts and first-observed-to-merge seconds are separate proxies. Commit trailers and explicit fix back-references give estimates, not proof of untouched code or defect causality. Untimed legacy costs require an exact provider-attempt match. Missing/incomplete measurements stay missing. PR history is capped at 1000 merges and attribution at fewer than 100 commits. Validate with `bash tests/test_baseline.sh`; all services are fake.

Linear authentication uses `LINEAR_API_KEY` after `.env` loading, overriding any inherited `API_KEY` alias. Recorded review rounds include interrupted reservations; approval reuse and gate polls without round metadata or provider receipts consume no round, while provider receipts without round metadata leave the measurement missing, even when their optional manifest links are absent. Fix titles accept the producer's optional ticket prefix before `fix:`, `bugfix:`, `hotfix:` or `revert:`; an explicit back-reference to an earlier merge is still required. References can match any earlier owned PR for a ticket; each fix PR counts once.

Receipt calls written by `bureau-provider.py:run` retain their provider stage and enclosing ticket/run/attempt identity: `spec-pipeline.sh` runs `research` inside `spec`; `bureau-config.sh:merge_origin_main_or_abort` runs `conflict_resolver` inside `ux`, `copy`, `implement`, `qa`, `code_review` and `merge`; `rebase-pipeline.sh` runs it inside `rebase`. `bureau-conflicts.py:evidence` writes resolver validation observations under the same enclosing attempts; these have no provider cost. Upstream port/summary calls create standalone attempts; valid records with `issue: null` are validated and counted separately from ticket measurements. Other stage relationships and mismatched identities are rejected, including unlinked receipts when their manifest is retained. Receipt coverage is per ticket: missing/corrupt receipts leave the affected cost missing and preserve unrelated estimates. Invalid records with no identifiable owner are counted separately; retained evidence cannot establish historical completeness.

### Measurement limits

Provider calls without retained receipts are not fully measured, so retained cost estimates can undercount. Manifests the report cannot validate can leave PR-linked measurements missing. Commits whose attribution it cannot verify are not counted as verified autonomous work; plain base merges require local objects and base refs plus Git 2.38+ to verify their parentage and automatic merge tree in a separate temporary store.

## What's out of scope

- **Rewriting bash to another language.** Bash is a constraint, not a bug. If you want a Rust rewrite, fork it.
- **Making Bureau backend-agnostic.** Linear is the source of truth. A Jira-backed variant is a fork, not a PR.
- **Adding a hosted application backend.** Bureau uses the existing app/terminal surfaces and local scripts.

## Communication

The project doesn't have a Slack / Discord / mailing list. Issues and PRs on GitHub are the communication surface. Keep it there.
