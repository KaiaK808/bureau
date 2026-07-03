# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`bureau-init` is itself a **Claude Code skill** — not a runtime. It does not build, test, or deploy. It is a bundle of template files that, when invoked via `/bureau-init` inside some *other* git repo, bootstraps a multi-agent Linear → spec → code → review → merge pipeline in that target repo.

No package manager. "Running" this project means installing it to `~/.claude/skills/bureau-init/` and then invoking `/bureau-init` from another directory. Changes to scripts here ship the next time a user runs the skill; they do not run locally.

A small shell-based test harness lives under `tests/` covering both the legacy iter-loop path and the `/goal`-driven path (under `BUREAU_USE_GOAL_LOOP=1`) for implement-pipeline, plus the `log_escalation` helper. Run `bash tests/run.sh` before committing changes to `templates/scripts/`. CI (`.github/workflows/test.yml`) runs the same suite on every push.

## Layout

```
SKILL.md                 # The 7-phase flow Claude follows when /bureau-init fires. Source of truth.
README.md                # User-facing install + usage doc for the generated pipeline.
docs/landscape.md        # Competitive landscape research (reference, not code).
templates/
  scripts/*.sh           # Pipeline scripts. Copied verbatim into target repo's scripts/.
  commands/*.md          # Bureau-init's own slash commands (linear-to-spec, check-linear-queue, …). Copied into target repo's .claude/commands/. Speckit commands are NOT here — they install via `specify init`.
```

Speckit is no longer vendored. Phase 4 of `/bureau-init` delegates to `specify init --here --integration claude --force` (pinned via `BUREAU_SPECKIT_VERSION` in SKILL.md, currently `v0.7.5`). That writes `.specify/` and `.claude/skills/speckit-*/SKILL.md` into the target repo. To bump the pin, run a spike (`specify init` in a throwaway dir + diff) before changing the constant.

The templates under `templates/scripts/` are the canonical, working versions of the pipeline — SKILL.md's Appendix B describes patterns but **the files in `templates/scripts/` are what actually ship**. When SKILL.md and a template disagree, the template wins; update SKILL.md to match.

## How the generated pipeline works

The pipeline scripts that `/bureau-init` copies into a target repo form a **Linear-state-machine worker**:

- Every script sources `bureau-config.sh`, which reads `.bureau.json` and exposes `$BUREAU_*` vars (team key, state UUIDs, label UUIDs, branch prefix, etc.) and a set of Linear-glue helpers.
- `queue-loop.sh` is the orchestrator. Each tick it picks an issue from one Linear state, hard-resets a dedicated worktree under `.worktrees/queue-<mode>/`, then invokes the corresponding `<stage>-pipeline.sh`.
- Stages flow `Triage → Spec → Spec Review → (Design) → Build → Build Review → Done`. Each stage has its own pipeline script and its own tmux window.

### Invariants when editing `templates/scripts/`

These are hard-learned rules (see the EXP-### tags in `bureau-config.sh` and SKILL.md Appendix B for the original incidents). Break them and the pipeline silently wedges in cron.

1. **Never spawn `$CLAUDE` for Linear CRUD.** Headless `claude -p` subprocesses can't refresh the remote Linear MCP's OAuth token (~1h TTL), so the first cron tick works and every subsequent tick fails silently. All Linear glue goes through helpers in `bureau-config.sh`: `pick_issue`, `move_issue`, `post_comment`, `get_issue_branch`, `get_issue_comments`, `get_issue_detail`, `get_issue_state`, `add_issue_label`. `$CLAUDE` is reserved for creative work (specify/plan/tasks, implementation, review prose).

2. **Branches are resolved via the `<!-- bureau-branch: ... -->` marker comment, not Linear's `branchName`.** The spec pipeline posts this marker on the first line of its spec-digest comment. Downstream pipelines parse it via `get_issue_branch`. Linear's auto-generated `branchName` does **not** match the sequential `001-*`, `002-*` spec branches.

3. **Preconditions run before state mutations.** Every pipeline calls `precondition_linear` (exit 10) and `precondition_claude_auth` (exit 16) *before* any `move_issue`. The spec pipeline additionally installs an EXIT trap immediately after its `Triage → Spec` move so a crash routes the issue back to Triage instead of stranding it.

4. **Exit codes are a protocol.** `queue-loop.sh` maps exit codes to alert classes and throttles Telegram alerts by `(issue, class)` per hour. Preserve the mapping:

   | 0 ok · 2 queue-empty · 10 linear-down · 11 worktree-dirty · 12 no-branch · 13 no-tasks · 14 build-failed · 15 no-pr · 16 claude-unauth |

5. **Worktrees are reset between picks.** `queue-loop.sh`'s `reset_worktree` fetches, resets to the correct ref (`origin/main` for spec, the issue's spec branch for everything else), and `clean -fdx`. Also calls `free_branch_from_other_worktrees` so two worktrees never try to hold the same branch (git refuses, exit 128).

6. **`pick_issue` filters by label *name*, not UUID.** This lets custom labels (`ai-implementable`, `needs-human`, `needs-ux`) work even when `.bureau.json` only captured the main `lane-2` label's UUID.

7. **The merge gate is strict.** `merge-pipeline.sh` enforces `pr_ci_is_green` and `pr_base_is_current` (in `bureau-config.sh`) **independently** of GitHub's `mergeStateStatus`, and re-runs the entire gate set just-in-time before `gh pr merge`. Don't weaken these. `mergeStateStatus == CLEAN` is async-cached and passes when no required checks are configured — relying on it caused a real incident where four PRs merged with red CI / stale base and broke main. The `.bureau.json` toggles `merge_require_green_ci` and `merge_require_up_to_date` exist for repos genuinely without CI (docs-only, prototypes). **Never flip them off as a debugging workaround** — the recurrence cost is "main goes red and nobody notices until a developer pulls."

## Editing SKILL.md

SKILL.md is the prompt Claude follows when a user invokes `/bureau-init` in *another* repo. It runs once interactively, so flow matters: Phase 0 (prereqs) → 1 (Linear discovery via MCP) → 2 (agent selection) → 3 (write `.bureau.json`) → 4 (speckit init via `specify` CLI) → 5 (copy scripts) → 6 (CLAUDE.md + .env) → 7 (validation). The final message must nudge the user to run `/speckit-constitution` — that's the one interactive step `/bureau-init` can't do itself.

When you change script behavior here, also update the corresponding Appendix B description in SKILL.md if it contradicts — otherwise future instances of Claude running `/bureau-init` will re-generate scripts from the stale description.

## Shipping changes

Changes here reach users two ways:
- `git pull` in `~/.claude/skills/bureau-init/` — picks up edits to this repo.
- A target repo re-running `/bureau-init --resync-scripts` — refreshes that repo's `scripts/` from the templates (per-file confirmation, local tweaks preserved). `--update` is config-only (teams/labels/states in `.bureau.json`) and does NOT touch scripts.

There is no version bump, no publish, no release. Commit with a conventional prefix (`fix(queue-loop):`, `fix(code-review):`, etc. — see recent `git log`).
