# Troubleshooting

When the pipeline misbehaves, start here. Most symptoms have a one-line fix.

---

## Setup

### `MISSING: <tool>` at Phase 0
Install the named tool, then re-run `/bureau-init`. macOS one-liner:

```sh
brew install jq tmux gh
```

### `This repo already has a bureau pipeline configured`
- `/bureau-init --update` — change teams / labels / states / agents
- `/bureau-init --resync-scripts` — pull the latest pipeline scripts
- `rm .bureau.json` — start from scratch

### Linear MCP not available during setup
In Claude Code, run `/mcp`, authenticate `linear-server`, restart the session.

### `LINEAR_API_KEY` not set
Interactive setup completes without it, but agents fail on first run. Add to `.env` before launching:

```sh
echo 'LINEAR_API_KEY=lin_api_xxxxxxxxxxxx' >> .env
```

---

## Pipeline runtime

### Agents running but not picking up issues
Check the issue:
- Has the eligibility label (e.g. `lane-2`) — `pick_issue` filters by label *name*
- Is in the `Triage` state for the team configured in `.bureau.json`
- Has no parking labels (`needs-human`, `blocked`, `wip`)
- Is not blocked by another issue (Linear `blockedBy` relation)

### Pipeline exits 2 every tick despite eligible issues
Check whether `agents.max_concurrent_issues` is set. If yes, an in-flight issue (or several) is occupying the cap. See [recipes → single-flight](recipes.md#single-flight-pipeline). Find them with:

```sh
grep "in-flight cap reached" logs/queue-spec.log
```

### `⚠ bureau-init template drift: N differ, M new`
The skill template has been updated since this repo was initialized. Run:

```sh
claude /bureau-init --resync-scripts
```

Per-file confirmation, ~40-line diff preview. Local tweaks preserved unless you confirm an overwrite.

### Issue stranded between states
The spec pipeline installs an EXIT trap that routes back to Triage on crash. If an issue is genuinely stuck:

1. Check `logs/escalations.log` first — every `needs-human` event lands here as one TSV line. `grep ESCALATED logs/escalations.log | grep EXP-XXX`.
2. Check `logs/queue-<stage>.log` for the last error.
3. Check `logs/supervisor-<stage>.log` if you're using `queue-loop-supervised.sh`.
4. Manually move the issue back in Linear.

If the issue was in Build and crashed, the worktree under `.worktrees/queue-implement/` may have uncommitted work. Inspect before resetting.

### Pipeline keeps hitting `merge_origin_main_or_abort` conflicts
Symptom: every cron tick re-runs the merge, conflicts pile up, the same branch reappears tick after tick.

Two complementary fixes:

1. **Single-flight mode** (`agents.max_concurrent_issues: 1`) — branches don't race against each other.
2. **Trivial-conflict resolver** — already shipped; lockfiles, generated files, and known-mergeable patterns auto-resolve.

For non-trivial conflicts, the pipeline exits 17 (rebase-needed) and routes for human intervention.

---

## Token-efficiency layers

### Implement parks `status=STUCK` on a ticket that's actually complete
Pre-`use_goal_loop` symptom: implement runs, sees the tests already ticked (e.g. QA wrote them in a prior cycle), self-reports `status=COMPLETE` with 0 commits, the cumulative override flips to STUCK. EXP-571 / EXP-624 history.

Fix: enable `/goal` loop. `jq '.agents.use_goal_loop = true' .bureau.json | sponge .bureau.json`. The branch-ahead commit check still catches truly empty COMPLETE claims, but bash no longer second-guesses a legitimate single-iter COMPLETE.

### `headroom: command not found` after `headroom_wrap: true`
The flag is read live; turning it on doesn't auto-install Headroom. Install on the host running the pipeline (typically the operator's local box, not the CI runner):

```sh
pip install "headroom-ai[all]"
headroom --version    # confirm
```

If `pip` resolves to a Python that doesn't have user-level installs on PATH, prefer `pip install --user` and add `~/.local/bin` to PATH, or use a venv. The pipeline's `claude_cmd_for_stage` doesn't probe — the first invocation surfaces the error.

### Caveman style leaked into a commit message or PR body
By design, caveman is scoped to per-stage review prose only — commit messages and PR titles/bodies stay in normal register. If you see telegraphic caveman-speak in commits or PRs, the leak path is via the per-stage prompt prefixes in `code-review-pipeline.sh` getting applied where they shouldn't.

Audit: `grep -n "caveman\|/caveman" templates/scripts/code-review-pipeline.sh templates/scripts/merge-pipeline.sh`. The prefix should only appear in review-prose construction sites. If it's in a commit-message or PR-body builder, that's the bug — file an upstream ticket and pin `caveman_level: "off"` until fixed.

### `/goal` is a no-op (turn loops forever or exits without evaluation)
Requires Claude Code v2.1.139+ locally. Check `claude --version`. On older builds the slash command is unrecognized and the prompt is treated as raw text — implement-pipeline still parses the JSON status block but the iter-loop replacement isn't actually happening.

Fix: upgrade Claude Code, OR flip `agents.use_goal_loop = false` to fall back to the bash for-loop (preserved verbatim for exactly this scenario).

---

## tmux

### `WARNING: Old 'bureau' session is still running`
Harmless. The script checks for a legacy session named literally `bureau` (no `-v2`). If you never ran the v1 pipeline, ignore it — or delete the check block at the top of `start-bureau-v2.sh`.

### tmux session already exists on startup
`start-bureau-v2.sh` kills its own session (`bureau-v2-<basename>`) before recreating it. If you see unrelated stale sessions:

```sh
tmux ls
tmux kill-session -t <name>
```

### Pane shrunk to 0 rows / "no space for new pane"
Already fixed — `start-bureau-v2.sh` re-tiles after every split. If you see this on an old install, run `/bureau-init --resync-scripts`.

---

## Linear state divergence

### PR is OPEN but Linear says Done
The verify-merge fix in `code-review-pipeline.sh` should prevent this — after `gh pr merge`, the pipeline confirms the actual PR state before claiming Done. If you see this on an old install, resync scripts.

### Issue marked Done but no PR exists
Check the bureau-branch comment marker. If it points at a branch that was deleted before merge, the pipeline may have lost the PR reference. Manually re-open the issue and route through Build Review.

---

## Claude / auth

### Pipeline fails with `claude-unauth` (exit 16)
Run `claude` interactively once to refresh OAuth. The headless `claude -p` calls used by the pipeline can't refresh tokens themselves.

### Headless Claude calls hang or timeout
Usually a network issue. Confirm:

```sh
claude -p "say hi" --print --dangerously-skip-permissions
```

If that hangs, restart Claude Code or check your connection.

### Speckit phases produce empty `tasks.md`
The spec pipeline routes back to Triage automatically. To debug:

```sh
ls -la specs/<NNN-feature>/
cat logs/queue-spec.log | tail -100
```

Most common cause: the Linear issue description was too thin for `/speckit-tasks` to extract anything. Add acceptance criteria to the description and re-trigger.

---

## Supervisor

### Telegram alert: "supervisor giving up"
The supervisor crashed `BUREAU_SUPERVISOR_MAX_CRASHES` times in a row. The alert includes the tail of `logs/queue-<mode>.log`. Common causes:

- Bash syntax error introduced by an unfinished script edit
- Missing config key after a partial `/bureau-init --update`
- `LINEAR_API_KEY` revoked

After fixing, restart with `./scripts/start-bureau-v2.sh` or just re-run `./scripts/queue-loop-supervised.sh <mode> <interval>`.

### Crash counter stuck high
Restart the supervisor — counters are in-process, so a fresh launch resets them. Or wait `BUREAU_SUPERVISOR_STABILITY_WINDOW` seconds (default 1 h) of clean runtime for the auto-reset.

---

## Drift / upgrades

### Old commands at `.claude/commands/speckit.*.md`
Pre-v0.7.5 speckit installed via slash commands. The new install lives at `.claude/skills/speckit-*/`. After running `/bureau-init --resync-speckit`, clean up:

```sh
rm .claude/commands/speckit.*.md
```

### Constitution missing after speckit resync
`--resync-speckit` backs up `.specify/memory/constitution.md` before re-running `specify init` and restores it after. If the file is still missing, look for `.specify/memory/constitution.md.bak`.

---

## See also

- [Configuration](configuration.md) — config keys + env vars
- [Exit codes](exit-codes.md) — what each failure code means
- [Recipes](recipes.md) — common config patterns
