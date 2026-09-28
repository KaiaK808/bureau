# Changelog

User-visible changes and upgrade actions are recorded here. Releases use dated version sections and immutable Git tags; see the [release process](docs/releases.md). Earlier changes on `main` were not tagged and remain listed separately below. Configuration schema numbers are not Bureau release versions.

An upgrade requires **updating the source skill and resyncing each adopting repository**. Neither `git pull` alone nor `/bureau-init --update` refreshes installed assets. Follow the [upgrade and conflict guide](docs/migration.md).

## [Unreleased]

## [3.0.0] - 2026-09-28

Stable release of the 3.0.0 candidates. Runtime, installer, templates and tests are identical to v3.0.0-rc.2; this release changes only the documentation of the release status. The changes since v2.0.0 are recorded in the 3.0.0-rc.2 and 3.0.0-rc.1 sections below, and the [v3.0.0 release notes](docs/release-notes.md) consolidate them. Validation added since rc.2: a second live acceptance in the maintainer's pilot installation on rc.2, which drove one ticket from Triage to Done with the shepherd and exercised `repo.post_implement_command`, the review build check, the merge gate and a relative `--worktree`.

**Upgrade:** from v3.0.0-rc.2 select tag `v3.0.0`; no resync is needed because the runtime is unchanged. From rc.1, v2.0.0 or a legacy copy, follow the [v3 upgrade section](docs/migration.md#upgrade-to-v3).

## [3.0.0-rc.2] - 2026-09-28

Second release candidate for Bureau v3.0.0, published as a GitHub prerelease. It carries the findings of the first live acceptance (a pilot installation resynced to rc.1 drove one ticket from Triage to Done with the shepherd) and of a check of seven tickets an installation had filed against its own pipeline scripts: the review verdict in one order ([#18](https://github.com/KaiaK808/bureau/pull/18)), needs-human escalations that survive a failed label write ([#17](https://github.com/KaiaK808/bureau/pull/17)), Linear answers checked in transport ([#19](https://github.com/KaiaK808/bureau/pull/19)), the shepherd outside the stages ([#20](https://github.com/KaiaK808/bureau/pull/20)), `repo.post_implement_command` and a final implement push that cannot be lost ([#21](https://github.com/KaiaK808/bureau/pull/21)), installation names anonymised ([#22](https://github.com/KaiaK808/bureau/pull/22)) and a 20-minute CI job limit ([#23](https://github.com/KaiaK808/bureau/pull/23)). From rc.1 the upgrade is a scripts resync; no configuration change is required. See [release notes](docs/release-notes.md) for the upgrade, compatibility and known limitations.

### Review verdict rules in one order

#### Fixed

- The review stage decides its verdict in one ordered step, `decide_review_verdict` in `bureau-config.sh`: verdict check, security count, the security specialist's CRITICAL count, security floor, build fold, and the cycle cap last. The cap used to run before the build fold, so a review whose reviewers approved while the build stayed red reached the cap as APPROVE, was folded to REQUEST_CHANGES afterwards and went round forever; it now escalates to `needs-human` at `agents.max_review_cycles`, and the review text says that only the build check stayed red and that the pipeline cannot tell code from environment (EXP-1514 in installation A). The security floor read a missing, negative or non-numeric `security_issues` as 0 and did nothing exactly when the review was unreliable; that is now BLOCK. A CRITICAL count above 0 in the security specialist's own json block is BLOCK whatever the merger chose, as the merger's own rules already say; other security findings still turn APPROVE into REQUEST_CHANGES (EXP-1518). The CRITICAL count is read from the whole security review, before the merge prompt trims long reviews: a provider envelope cut to its last KB is no longer JSON, and the count was lost. Counts of more than nine digits are not counts (2^63 and up made the shell comparison fail open). When the merger dropped its json verdict, the text form `REVIEW_VERDICT: X` now counts only an exact verdict word, so "NOT_APPROVED — BLOCK" is BLOCK instead of APPROVE (EXP-1513). The review cycle count is read before the paid review, and a failed read or an answer that cannot be counted ends the stage with 10 or 27 instead of counting cycle 0. **Upgrade:** none; a review that gives no readable `security_issues` count, or a text verdict that is not exactly one word, now ends BLOCK (exit 25) instead of APPROVE.

### A needs-human escalation survives a failed label write

#### Fixed

- A stage that hands a ticket to a human no longer treats a failed `needs-human` label write as a warning (EXP-1516). Before, the stage warned, posted its comment and often ended with 0, and the next pick took the same ticket again because the picker reads labels only, so a paid stage could run again on a ticket a human was meant to look at. Every site now goes through `mark_needs_human` (code review BLOCK, implement merge conflict and needs-human statuses, QA and UX NEEDS_HUMAN, merge failure, rebase lease rejection and conflicts): a failed write records the ticket in a local hold under `$(git rev-parse --git-common-dir)/bureau/needs-human-held/`, alerts, and the stage ends with 25 where it would have ended with 0 (a stage that already ends non-zero keeps its code). `pipeline_pick_next` skips held tickets and tries the label again on every pick; once the label is written the hold ends. The alert names the code the stage ends with. When Linear stayed unusable the ticket is held and the stage ends with 27; five arms used to swallow that 27: rebase conflicts, rebase lease rejection, merge failure, implement merge conflict and UX NEEDS_HUMAN (EXP-1482). A label that still cannot be written never fails a pick (one ticket must not stop every queue): it stays held and skipped, and each retry is a single attempt without waits. The holds and the pause marker are found through the repository that holds `.bureau.json`, not the current directory; a `BUREAU_CONFIG` outside any git repository falls back to the current directory's repository with a warning. QA removes its temp dir when it ends 25 for an unwritten label.
- `queue-loop.sh` reports a pick that fails with 27 (log line and alert, throttled per stage per hour) instead of reading it as an empty queue; any other pick failure is logged and still treated as empty. The picker's own notes (held and blocked tickets, retries) now reach the queue log instead of `/dev/null`.

**Upgrade:** none beyond the scripts resync. A ticket can only stay held when its label can never be written (for example no `needs-human` label for the team or the workspace); fix the label or delete the hold file. Runs that name the ticket explicitly (a shepherd) do not read the hold. An implement halt whose label could not be written now ends with 25; unpushed work in that worktree is preserved and later implement runs there end with 21 until a human resolves it (before, the 0 let the next reset discard it).

### Linear answers checked in transport (EXP-1482)

#### Fixed

- A Linear request now has a time limit (`--max-time` 30 s, `--connect-timeout` 10 s by default; `BUREAU_LINEAR_MAX_TIME` / `BUREAU_LINEAR_CONNECT_TIMEOUT` or `.linear.request.*`). Before, a hanging request never reached the retry ladder. A request cut off at the limit is `no-response` and is retried; with the defaults a Linear that stays unusable ends the stage with 27 after at most 220 s.
- An answer with an HTTP status outside 2xx is unusable even when its body is well-formed JSON. Before, an error page with the body `{"data":{}}` counted as a success, every reader answered "nothing" with exit 0, and the shepherd slept forever or walked past `needs-human`.
- `{"data":{}}` and a root field that is `null` without `errors` are unusable (`no-data`): every root field the template asks for is non-null in Linear's schema.
- A NUL byte in the answer makes it unusable (`not-json`). The shell used to drop NUL bytes while capturing, so a broken answer could reach the check already cleaned.
- The issue readers (`get_issue_detail`, `bureau_issue_snapshot` / `get_issue_state`, `get_issue_branch`, `get_issue_branch_and_comments`, `get_issue_comments`) require the list they read (labels, state, comments) and go through the retry ladder when it is missing, instead of reading a missing list as an empty one. A ticket without labels, without a branch marker, and an issue query that matches no ticket stay usable answers with exit 0.

Upgrade action: none. A repo whose Linear requests legitimately take longer than 30 s sets `BUREAU_LINEAR_MAX_TIME`. A limit of 0 is invalid (it would mean "no limit" to curl): the warning names the key and the next source applies, `.bureau.json` before the default. A halt path makes one attempt per write without retry waits, so a halt with N writes takes at most N × the time limit. Limits: the fault classes stay the four names the shepherd knows; a timeout is logged as "no answer within Ns" and classed `no-response`. A curl double in a test that prints no `-w` status line is judged by its body alone.

### Shepherd outside the stages (EXP-1482)

#### Fixed

- The shepherd's own moves (`--from-stage`, and the bump from Spec to Triage) no longer end it bare under `set -e` when they fail: `27` takes the Linear halt (alert with the fault class, one attempt each at `needs-human` and a halt comment), any other code halts with `1`, `needs-human` and a comment. `--from-stage` now moves the ticket after the shepherd claims it, so the queue keeps away from a ticket that just moved into a stage's waiting room.
- Ctrl-C or SIGTERM ends a shepherd run as cancelled (`130`): the claim is released and nothing else is written. Before, the INT/TERM trap only released the claim and the loop went on, so the next read or stage ran on a ticket nobody held; a SIGTERM sent to the shepherd alone waited out a 60 s sleep first. The shepherd's waits can now be cut short.
- The start check names its cause: a failed Linear check before the claim still ends with `10`, now with an alert naming the fault class, and writes nothing to the ticket.
- Linear answering without a state is no longer waited out forever: the fifth such answer in a row halts with `1`, `needs-human`, a comment and an alert.
- A moment-old read after a move no longer decides the next stage (EXP-1476): after `--from-stage` a read that does not yet show its target, and after the bump to Triage or a stage that returned `0` a read that still shows the state the ticket left, is read again up to three times, 5 s apart (`BUREAU_SHEPHERD_CONFIRM_SECONDS`, whole seconds; anything else falls back to 5 with a warning). A read that already shows the move costs no extra read and no wait; a ticket that really stayed where it was reaches the stuck detector 15 s later than before.
- A typo in `--from-stage` is refused before anything is claimed.
- A relative `shepherd.sh --worktree DIR` (the form its help recommends) is taken from the repo root and made absolute before it is handed on. Before, it reached `bureau-worker.sh` as it was; the worker changes into the worktree, so its cleanup ran `git -C <relative>` from there and the shepherd halted with `128` after a stage that had finished (found by the rc.1 pilot, EXP-1533). `bureau-worker.sh` now makes a relative worktree absolute itself too, for every caller. Workaround on rc.1: pass an absolute `--worktree`.

### From the brainhuggers-cli pilot

#### Added

- `repo.post_implement_command`: an optional hook the implement stage runs wherever it releases the work for review (a run that ends COMPLETE, whether or not it made commits of its own, and a PARTIAL run with commits, whose PR is marked ready; never in a dry run), after the loop and before the squash-range check and the final push. It is for a repository that derives files from an implementation, such as regenerated docs or command contracts, and replaces a local patch to `implement-pipeline.sh`. It runs in the implement worktree via `bash -o pipefail -c` with no stdin, `BUREAU_ISSUE` and `BUREAU_BRANCH` set, and a limit of `BUREAU_POST_IMPLEMENT_TIMEOUT` seconds (default 900, capped at the stage's total time, also accepted from `.env`); a timeout sends SIGTERM to its process group and SIGKILL to whatever is left after 5 s. It commits its own output, and it must be idempotent and exit 0 when there is nothing to commit (a bare `git commit` exits 1 then). A non-zero exit, a timeout, uncommitted changes it leaves (kept in the worktree, not deleted) or a HEAD that no longer contains the commit it started from (then nothing is pushed) halt the ticket like any other halt status and end the stage with `14`. Doctor reports the command and rejects a value that is not a string.

#### Fixed

- A failed final push in the implement stage no longer hands the ticket on while origin lacks commits. The push after the loop was non-fatal, so the PR was marked ready and the ticket moved to QA or Build Review with work that was not on origin. Now a failed push is retried once after 3 s; if it fails again, the branch is fetched from origin (a rejected push does not update the local `origin/<branch>`), and if `origin/<branch>..HEAD` then holds commits, or the fetch or the comparison fails, the stage says so on the ticket, with the hook's report when the hook failed too, and ends with `18` before any hand-off, and the worker keeps the worktree because it is ahead of origin. If origin already has every commit, the usual case after the per-iteration pushes, a failed push changes nothing and the stage goes on. The per-iteration pushes stay non-fatal. **Upgrade:** a driver that treated the implement stage's `0` as "pushed" sees `18` when origin refuses commits it does not have.

## [3.0.0-rc.1] - 2026-09-28

Release candidate for Bureau v3.0.0, published as a GitHub prerelease. It collects the hardening carried over from the installations ([#11](https://github.com/KaiaK808/bureau/pull/11)), source recording in the installer ([#12](https://github.com/KaiaK808/bureau/pull/12)), the review build check via `repo.test_command` ([#13](https://github.com/KaiaK808/bureau/pull/13)), fail-closed shepherd reads ([#14](https://github.com/KaiaK808/bureau/pull/14)) and the merge policy as configuration ([#15](https://github.com/KaiaK808/bureau/pull/15)). The major version marks the changed exit-code contract: a review BLOCK ends with `25` instead of `0`, a Linear that stays unusable ends a stage with the new code `27`, and the shepherd halts with an alert on every code except `0`, `2`, `10` and `16`. v3.0.0 follows once a pilot installation has qualified one ticket with this candidate. See the [v3.0.0-rc.1 release notes](docs/release-notes.md).

### Merge policy as configuration

#### Added

- `agents.merge_mode` (`"auto"` default, `"manual"`): with `manual` a human merges. The merge and rebase agents are off for every dispatcher; `merge-pipeline.sh` and `rebase-pipeline.sh` exit `2` before any Linear, gh or git call; an APPROVE parks the ticket in the Merge state; the shepherd ends at Merge with `20`. **`manual` needs `linear.teams[0].states.merge`**: without it the review stage refuses at its start with `24`, and doctor reports an error. The mode holds regardless of `agents.merge`, `agents.rebase` and the shepherd's forced stages; any value but exactly `auto` or `manual` falls closed to `manual` with a warning. `bureau-status.sh --config` and `bureau-doctor.py` (`merge_mode`) show the mode in effect. See the [recipe](docs/recipes.md#merge-by-hand).
- **Upgrade action:** installations that disabled merge and rebase by hand (an early `exit 2` at the top of both scripts, as in installation C and installation A) check that `linear.teams[0].states.merge` is set, then set `"agents": {"merge_mode": "manual"}` in `.bureau.json` **before resyncing** the scripts. The resync replaces the local block; without the key automatic merging is on again, and without the Merge state code review refuses with `24`.

#### Changed

- The shepherd and the queue loop no longer alert on a stage's exit `20` (stopped before merge) when `--no-merge` / `BUREAU_NO_MERGE` asked for it. A `20` nobody asked for still halts with an alert.

### Hardening carried over from the installations

The hardening list from installation A and installation B in `docs/2026-09-25-drift-inventar.md`, each item with a test that runs the real code and a negative control against the broken form. Two items were already closed by the v2 runtime (model values reach the provider as one argument; `reset_worktree` returns its status and finds `.env` and `.bureau.json` via the main checkout) and now have a test or a note instead of a change. **Resync each adopting repository** to receive them.

#### Changed

- A review BLOCK (and any unknown verdict) ends the review stage with exit `25` instead of `0`. `shepherd.sh` now halts with an alert on every exit code except `0`, `2`, `10` and `16`; codes it did not list before (`22` to `26`) used to stop without an alert.
- A Linear answer that is unusable (no response, not JSON, GraphQL `errors`, no `data`) is retried after 10, 30 and 60 seconds and then ends the stage with the new exit code `27` (`linear-unusable`); the shepherd halts, alerts with the fault class and tries `needs-human` and a halt comment once. Tune with `BUREAU_LINEAR_RETRIES` / `BUREAU_LINEAR_RETRY_WAIT_1..3` or `.linear.retry.*`.
- The implement stage no longer writes `[skip ci]` into iteration commits and no longer adds an empty "CI re-trigger" commit. With a PR open, each iteration's push now runs CI.
- `agents.max_concurrent_issues` counts leaf issues of the configured `linear.projects`: sub-issues count, epics and other projects do not.
- The cross-check of a new spec against open PRs runs under macOS's `/bin/bash` 3.2 and reports clean, conflicts or incomplete; anything but an explicit clean posts a warning on the ticket.

#### Fixed

- `.env` is parsed, never sourced, by all sixteen pipeline scripts.
- A red build can no longer soften a review BLOCK into rework.
- Squash and merge set their own sanitised subject and body, so no CI suppressor reaches `main`; the implement and QA stages halt (`CI_MARKER` / `needs-human`) when a commit message in the squash range carries one.
- Labels resolve to the issue's own team (then a workspace label), and a failed lookup is never reported as "no such label".
- An npm project's `node_modules` is restored after the worktree reset (`npm ci --ignore-scripts`, clone on identical manifests); a failure stops with `24` instead of building red.
- A failed implement push is reported with branch, exit code and git's output, and a detached HEAD pushes to `refs/heads/<branch>`.
- The review build check runs `repo.test_command`, then `scripts/bureau-test.sh`, then `npm run build`. The first two steps are the QA stage's; QA's further fallbacks (`npm test`, `cargo test`, `pytest`, `go test`) are not used in review. It used to run only `npm run build`, so a repo without `package.json` was never checked and the review comment still said "Passed"; now it says "not checked" and warns on stderr, and the verdict is left alone. A red check goes through the verdict floor like a red npm build; the command runs under pipefail as QA's does, and its status is the command's own, not that of a pipe into `tail`. New files the check leaves that git does not ignore, and tracked files it changes, are named on stderr, each with its own advice. **Upgrade:** an installation that ran another local script in the review build check sets `repo.test_command` to it before resyncing (installation A's `scripts/bureau-test.sh` is found as it is). A repo with both `repo.test_command` and `package.json` now runs the test command in review instead of `npm run build`. The command runs in the review worktree, so its output must be ignored by git.
- `shepherd.sh` no longer reads a failed Linear read as "no state", "no label" or "no branch" (EXP-1528). When its own read of the ticket's state, labels or branch gives up with `27`, it halts like a stage that gave up: alert with the fault class, then `needs-human` and a halt comment tried once each. Any other failed read halts with `1` and `needs-human`; a read cut short by Ctrl-C or SIGTERM ends as a cancelled run (`130`) and writes nothing. Before, an unusable Linear walked past `needs-human`, left the state loop waiting forever, ended a stage as `12` (no-branch) under the wrong name, and let `--dry-run` report success with the state "unknown". The label list is now read once per iteration instead of up to three times, and the dry run removes its fault file on every way out.

### Installer records its source

#### Added

- An asset `--apply` records in `.bureau-install.json` which Bureau source revision it installed from: under `sources`, per scope it wrote (`scripts`, `ci`, `workflows`, `interfaces/claude`, `interfaces/codex`), the exact tag or `null`, the nearest `git describe --tags`, the full commit and whether the source was dirty (checkout changes, or any file the batch reads that differs from its blob at that commit, gitignored files included); a source that is not its own Git checkout is recorded as `not a git checkout`. The preview prints the same `source` before anything is written. A preview or an apply stopped by a conflict records nothing, and an apply that finds the carried record stale drops it and records only the scopes it wrote.
- Doctor reports `template_source`: `recorded`, `source not recorded` (no manifest, one written before this change, or no scope recorded yet; not an error), or `stale` with a warning when file hashes changed after the record was written, as an installer without source recording leaves it once it changed a file.

Upgrade action: none. The manifest stays at version `1` and installers without source recording still read it; the next asset `--apply` records the source.

### Upgrade actions and known limitations

- Select source tag `v3.0.0-rc.1`, then resync the scripts scope in each adopting repository as one set, one pull request per repository. Set `agents.merge_mode: "manual"` (with a Merge state) and `repo.test_command` **before** the resync where they apply, make `.gitignore` cover what `repo.test_command` writes, and update repository tests that assert the old exit codes in the same pull request. See [Upgrade to v3](docs/migration.md#upgrade-to-v3).
- Exit code `24` (`environment-blocked`) already existed; it is now also used when an npm project's dependencies cannot be restored after the worktree reset and when `merge_mode` is `manual` without a Merge state. `27` (`linear-unusable`) is new. A read of the shepherd's own cut short by Ctrl-C or SIGTERM ends as a cancelled run (`130`).
- Validation: the full Linux/macOS suite, the installer fixtures and one independent verification pass per pull request #12–#15. No adopting repository has run this candidate on a live ticket yet.
- Known limitations: under `manual` nothing moves a ticket from Merge to Done after the merge by hand; a `manual` repository without a Merge state refuses review with `24` on every tick until it is configured; the review build check does not use the QA stage's further fallbacks; a genuinely empty ticket state still makes the shepherd retry every 60 seconds; a file Git ignores inside `templates/scripts` (such as `.env`) is copied into the target, and the source is then recorded as dirty.

## [2.0.0] - 2026-09-07

### Claude Code and Codex support

Bureau v2.0.0 includes the Claude/Codex integration and its review fixes ([#9](https://github.com/KaiaK808/bureau/pull/9)). It is the first versioned release; the public initial snapshot was labeled v1.0.0 but had no corresponding tag or GitHub Release. The major version marks the operational changes for existing workers and upgrades. Installing a source update still requires a target-repository resync. See the [v2.0.0 release notes](docs/release-notes-v2.0.0.md).

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

[Unreleased]: https://github.com/KaiaK808/bureau/compare/v3.0.0...main
[3.0.0]: https://github.com/KaiaK808/bureau/compare/v2.0.0...v3.0.0
[3.0.0-rc.2]: https://github.com/KaiaK808/bureau/compare/v3.0.0-rc.1...v3.0.0-rc.2
[3.0.0-rc.1]: https://github.com/KaiaK808/bureau/compare/v2.0.0...v3.0.0-rc.1
[2.0.0]: https://github.com/KaiaK808/bureau/compare/6763c26c26aa96a41a92fbe95416fddbf4d48f69...v2.0.0
