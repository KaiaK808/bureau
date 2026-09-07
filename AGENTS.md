# Maintaining Bureau

Bureau is an installer skill and template bundle, not an application service. `SKILL.md` routes setup/update/resync; `references/` contains conditional procedures; `scripts/bureau_install.py` installs assets deterministically. `templates/scripts/` is the authoritative background runtime copied to adopting repositories. `templates/commands/` is the canonical source for Claude commands and rendered Codex skills. `templates/instructions/` supplies managed project instructions.

There is no application package build or registry publication. Versioned source releases use reviewed tags and GitHub Release notes; see docs/releases.md. A user pulls this repository at their installed skill location and invokes a resync in a target repository. Paths must resolve from the actual skill, including symlinks and paths containing spaces. `--update` changes configuration only; resync scopes change assets. Preserve customized files, user instruction text and the Spec Kit constitution.

Run `bash tests/run.sh` before committing runtime or installer changes. CI also checks Bash syntax and ShellCheck warnings on the core pipelines. Keep tests dependency-light (Bash 3.2+, Python 3.9+, git, jq; Node 18+ for the shared scheduler); provider and network calls in tests must be faked. Inspect real output parsing instead of replacing it with a more permissive stub.

Spec Kit is installed by its pinned CLI, not vendored. `scripts/bureau_install.py` owns the 0.7.5 pin. Before changing it, initialize a throwaway repository and compare generated assets. Claude skills live in `.claude/skills`, Codex skills in `.agents/skills`; both can be installed while `.specify/integration.json` selects one active integration. Do not assume installing the second host switches the requested active one.

### Invariants when editing `templates/scripts/`

These are hard-learned rules (see the EXP-### tags in `bureau-config.sh` and references/runtime-patterns.md for the original incidents). Break them and the pipeline silently wedges in cron.

1. **Never spawn `$CLAUDE` for Linear CRUD.** Headless `claude -p` subprocesses can't refresh the remote Linear MCP's OAuth token (~1h TTL), so the first cron tick works and every subsequent tick fails silently. All Linear glue goes through helpers in `bureau-config.sh`: `pick_issue`, `move_issue`, `post_comment`, `get_issue_branch`, `get_issue_comments`, `get_issue_detail`, `get_issue_state`, `add_issue_label`. `$CLAUDE` is reserved for creative work (specify/plan/tasks, implementation, review prose).

2. **Branches are resolved via the `<!-- bureau-branch: ... -->` marker comment, not Linear's `branchName`.** The spec pipeline posts this marker on the first line of its spec-digest comment. Downstream pipelines parse it via `get_issue_branch`. Linear's auto-generated `branchName` does **not** match the sequential `001-*`, `002-*` spec branches.

3. **Preconditions run before state mutations.** Keep `precondition_linear` (exit 10) and applicable provider authentication checks (exit 16) before `move_issue`. Current coverage differs by stage; do not infer that every stage already checks authentication. The spec pipeline additionally installs an EXIT trap immediately after its `Triage → Spec` move so a crash routes the issue back to Triage instead of stranding it.

4. **Exit codes are a protocol.** `queue-loop.sh` maps exit codes to alert classes and throttles Telegram alerts by `(issue, class)` per hour. Preserve the mapping:

   | 0 ok · 2 queue-empty · 10 linear-down · 11 worktree-dirty · 12 no-branch · 13 no-tasks · 14 build-failed · 15 no-pr · 16 provider-unauth |

5. **Disposable worker worktrees are reset between picks.** `queue-loop.sh`'s `reset_worktree` fetches, resets to the correct ref (`origin/main` for spec, the issue's spec branch for everything else), and `clean -fdx`. Claims issue/workspace ownership first; `free_branch_from_other_worktrees` reports held branches without detaching them. Existing unregistered checkouts may not be reset. App stages use the current-workspace prepare/finish protocol.

6. **`pick_issue` filters by label *name*, not UUID.** This lets custom labels (`ai-implementable`, `needs-human`, `needs-ux`) work even when `.bureau.json` only captured the main `lane-2` label's UUID.

7. **The merge gate is strict.** `merge-pipeline.sh` enforces `pr_ci_is_green` and `pr_base_is_current` (in `bureau-config.sh`) **independently** of GitHub's `mergeStateStatus`, and re-runs the entire gate set just-in-time before `gh pr merge`. Don't weaken these. `mergeStateStatus == CLEAN` is async-cached and passes when no required checks are configured — relying on it caused a real incident where four PRs merged with red CI / stale base and broke main. The `.bureau.json` toggles `merge_require_green_ci` and `merge_require_up_to_date` exist for repos genuinely without CI (docs-only, prototypes). **Never flip them off as a debugging workaround** — the recurrence cost is "main goes red and nobody notices until a developer pulls."

## Shipping

Use conventional commit prefixes. User-facing changes need an Unreleased changelog entry with upgrade actions and compatibility/validation limits. Follow docs/releases.md for release preparation; tagging/publishing is an explicit maintainer operation, separate from implementation or documentation edits. Keep shared maintenance rules here; CLAUDE.md imports this file. Document changed behavior alongside the templates and update contradictory runtime-pattern examples. Runtime scripts win over prose.

Keep new app/current-workspace operations distinct from disposable-worker resets. A task may start on a detached worktree outside `.worktrees/`, and the shared Git directory can contain user-owned checkouts. Never claim full Codex background support based solely on installation/discovery tests.
