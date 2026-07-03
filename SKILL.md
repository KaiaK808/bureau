---
name: bureau-init
description: Bootstrap a multi-agent pipeline in any repo — Linear integration, speckit, tmux agents, queue workers
trigger: /bureau-init
---

# /bureau-init

Bootstrap a full multi-agent CI/CD pipeline in any git repository. Connects to Linear for issue tracking, initializes spec-kit for spec-driven development, and generates all scripts needed to run autonomous pipeline agents in tmux.

## Usage

```
/bureau-init                    # interactive setup in current repo
/bureau-init --update           # re-run in existing repo (update mode)
/bureau-init --resync-scripts   # pull in latest pipeline scripts only
/bureau-init --resync-workflows # pull in latest planning-brain workflows only
/bureau-init --resync-ci        # pull in / refresh the scaffolded .github/workflows/ci.yml
/bureau-init --resync-speckit   # re-run `specify init` for the pinned spec-kit version
/bureau-init --help             # open the HTML docs in the default browser
```

## What You Must Do When Invoked

Follow these phases in order. Each phase builds on the previous one.

---

### Phase 0 — Prerequisites Check

Before anything else, verify the environment:

```bash
# Check required tools
for cmd in git jq tmux claude curl; do
  command -v "$cmd" &>/dev/null || echo "MISSING: $cmd"
done

# Check we're in a git repo
git rev-parse --is-inside-work-tree 2>/dev/null || echo "MISSING: not a git repo"
```

**Linear access:** Check if Linear MCP tools are available (try calling `list_teams`). If MCP works, use it for all discovery in Phase 1 — no API key needed for setup. If MCP is not available, fall back to checking `LINEAR_API_KEY`.

**Important:** The headless pipeline scripts (queue-loop.sh, etc.) always need `LINEAR_API_KEY` in `.env` because they run outside Claude Code without MCP. If the key isn't set yet, note it as a post-setup step — don't block the interactive setup on it.

If tools or git are missing, tell the user exactly what to install and stop.
Do NOT proceed without git and a git repo.

**Fast path — `--help`:** If `$ARGUMENTS` contains `--help` (or `-h`), open the bundled HTML docs in the user's default browser and stop. Handle this *before* every other phase check — `--help` works regardless of whether `.bureau.json` exists, regardless of which directory the user is in.

```bash
DOCS_INDEX="$HOME/.claude/skills/bureau-init/docs/site/index.html"
if [ ! -f "$DOCS_INDEX" ]; then
  echo "ERROR: bundled HTML docs not found at $DOCS_INDEX"
  echo "       Reinstall the skill or pull the latest from the repo."
  exit 1
fi
# Cross-platform opener: macOS uses `open`, most Linux desktops use
# `xdg-open`, WSL uses `wslview`. Try them in order; print the path as a
# fallback if none are available (headless / SSH session).
if command -v open       >/dev/null 2>&1; then open       "$DOCS_INDEX"
elif command -v xdg-open >/dev/null 2>&1; then xdg-open   "$DOCS_INDEX"
elif command -v wslview  >/dev/null 2>&1; then wslview    "$DOCS_INDEX"
else
  echo "No graphical browser opener found. Open this URL manually:"
  echo "  file://$DOCS_INDEX"
fi
echo "Opened: $DOCS_INDEX"
```

After the opener runs, also print a one-line index of section landmarks so terminal-only users get something useful:

```
bureau-init docs ($DOCS_INDEX):
  index.html          — landing page + quickstart + pipeline architecture
  configuration.html  — full .bureau.json + BUREAU_* env var reference
  exit-codes.html     — exit code table + Telegram alerts + supervisor
  recipes.html        — single-flight, mixed models, dry-run, memory loop
  troubleshooting.html — symptom → fix
```

Then stop. Never fall through.

**Fast path — `--resync-scripts`:** If `$ARGUMENTS` contains `--resync-scripts`, handle it *before* the normal `.bureau.json` gate. Never fall through to Phase 1+ in this mode.

**Scope:** this mode only touches `./scripts/*.sh`. It does NOT touch `.bureau.json`, `.specify/`, `.claude/skills/speckit-*`, or `.claude/commands/`. Those belong to `--update` and `--resync-speckit`.

1. **Prerequisite check**
   - `.bureau.json` must exist in the current repo. If missing, tell the user to run `/bureau-init` first and stop.
   - `~/.claude/skills/bureau-init/templates/scripts/` must exist. If missing, tell the user to reinstall the skill and stop.

2. **Classify every script**
   - For each `*.sh` in `~/.claude/skills/bureau-init/templates/scripts/`:
     - If `./scripts/<name>` does not exist → **missing** (new in template)
     - Else if `cmp -s ./scripts/<name> ~/.claude/skills/bureau-init/templates/scripts/<name>` → **identical**
     - Else → **drifted**
   - Also list any `./scripts/*.sh` that has no template equivalent → **custom** (never modified)

3. **Handle drifted files** (one at a time, in stable sort order)
   - Show a condensed unified diff: `diff -u ./scripts/<name> ~/.claude/skills/bureau-init/templates/scripts/<name> | head -40`
   - Ask: **"Overwrite `scripts/<name>`? (y/N)"** — default **No**
   - On `y` / `yes` → `cp` template over the repo file, then `chmod +x ./scripts/<name>` to preserve exec bit
   - On anything else → skip and move on

4. **Handle missing files** (new in template since last init)
   - Tell the user: "New template script available: `scripts/<name>`"
   - Show the first ~15 lines of the template so they can see what it does
   - Ask: **"Install it? (Y/n)"** — default **Yes** (new scripts are usually wanted for pipeline completeness)
   - On yes / `<enter>` → `cp` + `chmod +x`
   - On `n` / `no` → skip

5. **Never delete** custom scripts in the repo that have no template equivalent. Just note them at the end.

6. **Summary report** (always print, even when nothing changed):
   ```
   Script resync complete.
     Synced:    N  (drifted → template)
     Installed: K  (new from template)
     Skipped:   M  (you declined)
     Identical: L  (already up to date)
     Custom:    C  (no template equivalent — left untouched)
   ```

7. **Per-agent model prompt** (always, even when nothing changed)
   - Run `bash scripts/bureau-status.sh --config 2>/dev/null | sed -n '/MODELS/,/RUNTIME/p'` and surface it.
   - Ask: **"Review per-agent models now? (y/N)"** — default **No**.
     - On `y` / `yes` → run the **Per-agent model selection** routine (see
       Update Mode section). The routine writes only `.bureau.json`, never
       touches `./scripts/`.
     - On anything else → skip; remind: "You can run `/bureau-init --update` and pick the Models option group later."
   - Rationale: script resync may bring in new `claude_cmd_for_stage` consumers
     (e.g., a pipeline that previously hard-coded `claude -p`). The freshly-
     synced pipeline can now honor a per-stage `.agents.<stage>.model` that
     was silently ignored before — and the operator should explicitly choose
     instead of inheriting whatever default the previous drift accidentally
     set.

Then stop.

**Fast path — `--resync-workflows`:** If `$ARGUMENTS` contains `--resync-workflows`, handle it *before* the normal `.bureau.json` gate. Never fall through to Phase 1+ in this mode.

**Scope:** this mode only touches `./.claude/workflows/*.js` — the bundled planning brains (currently `conflict-aware-schedule`, the scheduler that feeds `orchestrate.sh --execute`). It does NOT touch `./scripts/`, `.bureau.json`, `.specify/`, or `.claude/commands/`.

1. **Prerequisite check**
   - `~/.claude/skills/bureau-init/templates/workflows/` must exist. If missing, tell the user to reinstall the skill and stop.

2. **Classify every workflow**
   - For each `*.js` in `~/.claude/skills/bureau-init/templates/workflows/`:
     - If `./.claude/workflows/<name>` does not exist → **missing** (new in template)
     - Else if `cmp -s ./.claude/workflows/<name> ~/.claude/skills/bureau-init/templates/workflows/<name>` → **identical**
     - Else → **drifted**
   - Also list any `./.claude/workflows/*.js` with no template equivalent → **custom** (never modified, never deleted)

3. **Handle drifted** (one at a time): show `diff -u ./.claude/workflows/<name> <template> | head -40`, ask **"Overwrite `.claude/workflows/<name>`? (y/N)"** (default No), `cp` on yes.

4. **Handle missing** (new in template): show the first ~15 lines, ask **"Install it? (Y/n)"** (default Yes), `mkdir -p ./.claude/workflows && cp` on yes.

5. **Never delete** custom workflows with no template equivalent — just note them.

6. **Summary report** (always): `Synced: N · Installed: K · Skipped: M · Identical: L · Custom: C`.

7. **Usage hint** (when `conflict-aware-schedule` was installed/synced): "Run it on a backlog (`[{ticket,title,summary}]`) to emit `{parallelSafe, serialChains}`; save that to `schedule.json` and run `scripts/orchestrate.sh --execute --schedule schedule.json`."

Then stop.

**Fast path — `--resync-ci`:** If `$ARGUMENTS` contains `--resync-ci`, handle it *before* the normal `.bureau.json` gate. Never fall through to Phase 1+ in this mode.

**Scope:** this mode only touches `./.github/workflows/ci.yml` — the scaffolded CI workflow (defaults to `runs-on: ubuntu-latest`; a private non-production self-hosted runner is a documented opt-in). It does NOT touch `./scripts/`, `./.claude/workflows/`, `.bureau.json`, `.specify/`, or any other workflow file the repo may have.

1. **Prerequisite check**
   - `~/.claude/skills/bureau-init/templates/.github/workflows/ci.yml` must exist. If missing, tell the user to reinstall the skill and stop.

2. **Classify** `./.github/workflows/ci.yml`:
   - Does not exist → **missing**
   - `cmp -s ./.github/workflows/ci.yml ~/.claude/skills/bureau-init/templates/.github/workflows/ci.yml` → **identical**
   - Else → **drifted**

3. **Handle drifted:** show `diff -u ./.github/workflows/ci.yml ~/.claude/skills/bureau-init/templates/.github/workflows/ci.yml | head -40`, ask **"Overwrite `.github/workflows/ci.yml`? (y/N)"** — default **No** (repos usually customize the test command + `runs-on`). `cp` on yes.

4. **Handle missing:** show the first ~15 lines of the template, ask **"Install a CI workflow? Defaults to `runs-on: ubuntu-latest` (GitHub-hosted, safe for public repos). Swap to your own `[self-hosted, ...]` labels only if the runner is private + non-production — self-hosted runners on public repos are a fork-PR-RCE class. (Y/n)"** — default **Yes**. On yes → `mkdir -p ./.github/workflows && cp` the template in.

5. **Never delete** or modify any other file under `./.github/` — only `ci.yml` is managed.

6. **Summary report** (always): `Synced: N · Installed: K · Skipped: M · Identical: L`. When installed or synced, remind: "Replace the `bash tests/run.sh` placeholder with your repo's real build/test command. If you swap `runs-on` to a self-hosted runner, read the security block at the bottom of the template first."

Then stop.

**Fast path — `--resync-speckit`:** If `$ARGUMENTS` contains `--resync-speckit`, handle it *before* the normal `.bureau.json` gate. Never fall through to Phase 1+ in this mode.

**Scope:** this mode reinstalls speckit at the Bureau-pinned version (`BUREAU_SPECKIT_VERSION`). It overwrites `.specify/templates/`, `.specify/scripts/`, `.specify/integrations/`, `.specify/workflows/`, and `.claude/skills/speckit-*/SKILL.md`. It backs up + restores `.specify/memory/constitution.md`. It does NOT touch `.bureau.json`, pipeline scripts, or Bureau's own commands in `.claude/commands/`.

1. **Prerequisite check**
   - `.bureau.json` must exist; if missing, tell the user to run `/bureau-init` first and stop.
   - `uv` and `specify` must be available; if either is missing, follow Phase 4 Step 4a installer guidance and stop.

2. **Verify `specify` is at the pinned version** (or upgrade silently):
   ```bash
   uv tool install specify-cli --force --from "git+https://github.com/github/spec-kit.git@${BUREAU_SPECKIT_VERSION}"
   ```

3. **Back up the constitution** (upstream Force overwrites it):
   ```bash
   [ -f .specify/memory/constitution.md ] && cp .specify/memory/constitution.md /tmp/bureau-constitution-backup.md
   ```

4. **Run `specify init --here --integration claude --force --no-git --ignore-agent-tools`.** Capture stdout for the summary report.

5. **Restore the constitution:**
   ```bash
   [ -f /tmp/bureau-constitution-backup.md ] && mv /tmp/bureau-constitution-backup.md .specify/memory/constitution.md
   ```

6. **Detect and warn about stale legacy commands.** If `.claude/commands/speckit.*.md` files still exist (from a pre-migration bureau-init install), they will appear as duplicate slash commands alongside the new `/speckit-X` ones. Print:
   > "Stale legacy speckit commands detected: \<list>. These shadow the new `/speckit-X` skills. Run `rm .claude/commands/speckit.*.md` to clean up."
   Do not delete them yourself — operator decides.

7. **Summary report:**
   ```
   Speckit resync complete (pinned to v0.7.5).
     .specify/templates/   refreshed
     .specify/scripts/     refreshed
     .claude/skills/speckit-*/  refreshed
     constitution.md       preserved
     Legacy commands:      <none | list>
   ```

Then stop.

If `.bureau.json` already exists and `$ARGUMENTS` does not contain `--update`, `--resync-scripts`, or `--resync-speckit`, tell the user:
> "This repo already has a bureau pipeline configured. Run `/bureau-init --update` to modify the config, `/bureau-init --resync-scripts` to pull in template script updates, `/bureau-init --resync-speckit` to refresh spec-kit at the pinned version, or delete `.bureau.json` to start fresh."

Then stop.

---

### Phase 1 — Linear Discovery (interactive)

Use **Linear MCP tools** for all discovery. These are already authenticated in the Claude Code session — no API key needed.

If MCP tools are not available (no `mcp__linear-server__*` tools), fall back to `curl` + `LINEAR_API_KEY` from `.env`.

**Step 1a: Discover teams**

Call `mcp__linear-server__list_teams` to get all teams. Present as a numbered list.

Ask: **"Which team(s) should the pipeline listen to? (comma-separated numbers)"**

**Step 1b: Discover projects**

For each selected team, call `mcp__linear-server__list_projects` and filter by team.

Present projects with their status. Ask: **"Which projects should the pipeline pull from? (comma-separated numbers, or 'all' for all started projects)"**

**Step 1c: Discover labels**

Call `mcp__linear-server__list_issue_labels` for each selected team.

Ask: **"Which label marks issues as AI-eligible? (the pipeline only picks up issues with this label)"**
Default suggestion: look for "lane-2" or similar. If none exists, offer to create one using `mcp__linear-server__create_issue_label`.

Also ask about additional labels:
- **needs-human** label (for escalation) — find or create
- **needs-ux** label (for UI routing) — find or create
- **ai-implementable** label (alternative eligibility) — find or create
- **needs-copy** label (optional — for copywriter routing; only prompt if user enabled the `copy` agent in Phase 2)
- **rebase-needed** label (optional — set by merge-pipeline.sh when a PR is wedged on `mergeStateStatus=DIRTY` *and* the divergence is bureau-only, so the kanban surfaces "wedged on rebase" vs "wedged on review/CI". Pure visibility — the rebase agent picks the PR up regardless of the label. Only prompt if the user enabled the `merge` or `rebase` agent in Phase 2.)

**Step 1d: Discover workflow states**

Call `mcp__linear-server__list_issue_statuses` for each selected team.

Present the states and ask the user to map them to pipeline stages. The pipeline needs these states:

| Pipeline stage | Default state name | Purpose | Required? |
|---|---|---|---|
| Entry | Triage | Where new issues land | yes |
| Spec | Spec | Issue is being spec'd | yes |
| Spec Review | Spec Review | Spec is being validated | yes |
| Design | Design | UI/UX artifacts being generated | only if `ux` agent enabled |
| Copy | Copy | Copywriter polishes user-facing strings | only if `copy` agent enabled (opt-in) |
| Build | Build | Implementation in progress | yes |
| QA | QA | Test suite runs, missing tests added | only if `qa` agent enabled (opt-in) |
| Build Review | Build Review | Code review in progress | yes |
| Merge | Merge | Approved PR awaiting gated merge | only if `merge` agent enabled (opt-in) |
| Done | Done | Completed and merged | yes |

For each required state, try to auto-match by name. If a state doesn't exist, ask the user if they want to create it. States can be created via the Linear web UI or API — note which ones need to be created and remind the user.

**QA, Copy, and Merge are opt-in.** Only prompt for these states if the user enabled the corresponding agent in Phase 2. The pipeline scripts (`qa-pipeline.sh`, `copy-pipeline.sh`, `merge-pipeline.sh`, `rebase-pipeline.sh`) exit 2 (queue-empty) when their state UUIDs aren't present in `.bureau.json`. The `rebase` agent reuses `BUREAU_STATE_MERGE` — no separate state needed.

State type mapping for reference: Triage→triage, Spec/Spec Review/Design/Copy/QA/Build/Build Review/Merge→started, Done→completed

**Step 1e: LINEAR_API_KEY for headless scripts**

The pipeline scripts use direct Linear API calls for fast, cheap operations (state validation, issue moves, cycle counting) instead of spawning Claude sessions. This needs an API key.

Check if `LINEAR_API_KEY` is available:

```bash
([ -f .env ] && grep -q LINEAR_API_KEY .env) && echo "KEY_FOUND" || echo "KEY_MISSING"
```

If missing, show this guide:

> **Linear API key setup**
>
> The pipeline agents use MCP for complex work (picking issues, writing specs) but direct API calls for quick operations (validating state, moving issues). This avoids spending tokens on trivial actions.
>
> To get your key:
> 1. Go to https://linear.app/settings/account/api
> 2. Click "Create key", name it "bureau-pipeline"
> 3. Copy the key (starts with `lin_api_`)
> 4. Add it to `.env` in this repo:
>    ```
>    echo 'LINEAR_API_KEY=lin_api_YOUR_KEY_HERE' >> .env
>    ```
>
> The key is the same across all repos in your Linear workspace — you only create it once.

Do NOT block setup on this — the `.bureau.json` and scripts can be generated without it. The key is only needed when `./scripts/start-bureau-v2.sh` runs.

---

### Phase 2 — Agent Selection (interactive)

Ask the user which pipeline agents to enable:

```
Available pipeline agents:

  1. spec          — Picks Triage issues, runs specify → plan → tasks
  2. spec-review   — Validates specs against codebase, routes to Build or Design
  3. ux            — Generates design.md for UI-heavy issues (optional, opt-in)
  4. copy          — Polishes user-facing strings against a voice guide (optional, opt-in)
  5. implement     — Executes tasks, creates commits and PRs
  6. qa            — Mechanical test runner: runs the suite, writes missing tests (optional, opt-in)
  7. code-review   — Reviews PRs (multi-specialist: correctness + security + performance)
  8. merge         — Closes the loop: gated PR merger for already-approved PRs (optional, opt-in)
  9. rebase        — Auto-rebases DIRTY bureau-only branches; force-pushes (optional, opt-in, OFF by default)

Which agents should run? (comma-separated numbers, default: 1,2,5,7)

QA vs code-review (these are complementary, not redundant):
- qa is a MECHANICAL executor — runs `npm test` / `cargo test`, writes test files, commits them. Catches missing coverage and broken tests.
- code-review is a JUDGMENTAL reviewer — reads the diff, writes a PR comment, never touches the branch. Catches logic bugs, security, spec drift.

Copy is opt-in and only useful for repos with significant user-facing UI. It reads a voice guide (markdown file you point at via `copy_voice_file` in .bureau.json) and edits button labels, error messages, empty states, etc.

Merge closes the loop. Without it, code-review APPROVE → squash-merge happens inline in code-review-pipeline.sh. With merge enabled, code-review APPROVE moves the issue to a new "Merge" Linear state and merge-pipeline.sh picks up from there with extra gates: PR must be OPEN, mergeStateStatus must be CLEAN, latest verdict must still be APPROVE / AUTO_APPROVE, no unresolved review threads, no needs-human/blocked/wip label. Enabling merge does NOT auto-approve — review must still pass first.

Rebase is OFF by default because it force-pushes (mutates shared remote state). It only fires when mergeStateStatus is DIRTY (real conflict, not BEHIND) AND every commit ahead of main carries a Co-authored-by:Claude trailer. If a human commit is in the divergence, it skips and posts a comment.
```

Also ask:
- **Poll interval**: "How often should agents check for work? (minutes, default: 30)"
- **Workbench panes**: "How many interactive Claude sessions in the workbench? (default: 2)"
- **Branch prefix**: "Branch prefix for feature branches? (default: feat)" — suggest based on team key

---

### Phase 3 — Write .bureau.json

Generate the config file from all collected information:

```json
{
  "version": 1,
  "linear": {
    "teams": [
      {
        "key": "CLI",
        "id": "team-uuid",
        "name": "Brainhuggers CLI",
        "states": {
          "triage": "state-uuid",
          "spec": "state-uuid",
          "spec_review": "state-uuid",
          "design": "state-uuid",
          "copy": "state-uuid",
          "build": "state-uuid",
          "qa": "state-uuid",
          "build_review": "state-uuid",
          "merge": "state-uuid",
          "done": "state-uuid"
        }
      }
    ],
    "labels": {
      "lane2": { "id": "label-uuid", "name": "lane-2" },
      "needs_human": { "id": "label-uuid", "name": "needs-human" },
      "needs_ux": { "id": "label-uuid", "name": "needs-ux" },
      "needs_copy": { "id": "label-uuid", "name": "needs-copy" },
      "ai_implementable": { "id": "label-uuid", "name": "ai-implementable" }
    },
    "projects": ["project-uuid-1", "project-uuid-2"]
  },
  "agents": {
    "spec": true,
    "spec_review": true,
    "ux": false,
    "copy": false,
    "implement": true,
    "qa": false,
    "code_review": true,
    "merge": false,
    "rebase": false,
    "poll_interval_minutes": 30,
    "workbench_panes": 2,
    "max_review_cycles": 3,
    "use_goal_loop": false,
    "headroom_wrap": false,
    "caveman_level": "off"
  },
  "repo": {
    "branch_prefix": "feat",
    "commit_prefix": "CLI",
    "specs_dir": "specs",
    "copy_voice_file": "docs/copy-voice.md"
  }
}
```

Write the file. Then add `.bureau.json` to `.gitignore` if not already there (it contains UUIDs that are workspace-specific).

**Token-efficiency flags** (`use_goal_loop`, `headroom_wrap`, `caveman_level`) default to OFF in the example above. Don't prompt the user during initial /bureau-init — they're per-repo opt-in via `.bureau.json` edits after setup. Full documentation: `docs/token-efficiency.md` and the `.agents.use_goal_loop` / `.agents.headroom_wrap` / `.agents.caveman_level` entries in `docs/configuration.md`. Phase 6e below performs the install side-effects when these flags are turned on at setup time.

---

### Phase 4 — Speckit Initialization (delegated to `specify` CLI)

bureau-init no longer ships a vendored speckit snapshot. Instead it delegates to the upstream `specify` CLI, pinned to a known-good release. This keeps us off the vendoring treadmill and gets the v0.4.5+ native-skills install (`.claude/skills/speckit-*/SKILL.md`) for free.

**Pinned version:** `v0.7.5` — the `BUREAU_SPECKIT_VERSION` constant below. Bump per bureau-init release after re-running the spike (`specify init` in a throwaway dir + diff).

```bash
BUREAU_SPECKIT_VERSION="v0.7.5"
```

**Step 4a: Verify `uv` and `specify` are installed**

```bash
command -v uv      || echo "MISSING: uv (install: curl -LsSf https://astral.sh/uv/install.sh | sh)"
command -v specify || echo "MISSING: specify"
```

If `specify` is missing OR its version differs from the pin, install/upgrade it:

```bash
uv tool install specify-cli --force --from "git+https://github.com/github/spec-kit.git@${BUREAU_SPECKIT_VERSION}"
```

If `uv` itself is missing, stop and tell the user to install it (link above). Do not proceed.

**Step 4b: Check if speckit is already set up**

```bash
[ -d .specify ] && echo "EXISTS" || echo "MISSING"
```

**Step 4c: Initialize (or refresh) speckit via the CLI**

The `specify init --here --force` invocation will overwrite `.specify/memory/constitution.md` (known upstream issue) — back it up first if it exists.

```bash
[ -f .specify/memory/constitution.md ] && cp .specify/memory/constitution.md /tmp/bureau-constitution-backup.md

specify init --here --integration claude --force --no-git --ignore-agent-tools

[ -f /tmp/bureau-constitution-backup.md ] && mv /tmp/bureau-constitution-backup.md .specify/memory/constitution.md
```

This refreshes (against the v0.7.5 pin) — note v0.7.5+ **preserves** existing shared files
rather than blindly overwriting; the per-file SHA256 manifest at
`.specify/integrations/*.manifest.json` is what gates updates:

- `.specify/memory/constitution.md` (template — only installed if missing; existing
  customized constitutions are preserved)
- `.specify/templates/{spec,plan,tasks,checklist,constitution}-template.md` (shared
  files; preserved if locally modified per the integration manifest)
- `.specify/scripts/bash/{common,check-prerequisites,create-new-feature,setup-plan}.sh`
- `.specify/integrations/{claude,speckit}.manifest.json` (per-file SHA256 — drives
  clean upgrade tracking, decides what to refresh vs preserve)
- `.specify/workflows/speckit/workflow.yml` (interactive single-feature workflow — bureau-init doesn't use this; our queue-loop replaces it)
- `.claude/skills/speckit-{specify,plan,tasks,implement,analyze,checklist,clarify,constitution,taskstoissues}/SKILL.md` (native Claude Code skills — invokable as `/speckit-specify` etc.)
- `CLAUDE.md` at repo root (will be merged with Bureau's CLAUDE.md content in Phase 6)

Do NOT also copy anything from `~/.claude/skills/bureau-init/templates/` for speckit — Bureau no longer ships any speckit assets.

**Step 4d: Install Bureau's Linear integration commands**

These are Bureau's own slash commands, not speckit's. Copy from `~/.claude/skills/bureau-init/templates/commands/` to `.claude/commands/`:

- `check-linear-queue.md`
- `check-implement-queue.md`
- `linear-to-spec.md`
- `linear-implement.md`
- `bureau-learnings.md`

These read `.bureau.json` for team/state/label configuration. If `.claude/commands/` already has these files, ask before overwriting.

`bureau-learnings.md` mines `logs/events.jsonl` (auto-created by `queue-loop.sh` on first event) plus Linear comments to draft a human-curated `LESSONS.md`. The spec and code-review pipelines selectively include `LESSONS.md` in their prompts. Run weekly after the queue has activity.

**Step 4e: Create specs directory**

```bash
mkdir -p specs
```

**Step 4f: Constitution setup reminder (conditional)**

The constitution is an interactive workflow that needs user input — it can't run inside bureau-init. **Only show this reminder if the constitution is still the unfilled template** — don't ask users with a real, curated constitution to overwrite it.

Detect the template vs. real-content state:

```bash
# Heuristic: a curated constitution has been edited beyond the template
# placeholders. The template ships with `<<` / `>>` placeholder markers and
# the literal heading "PROJECT CONSTITUTION TEMPLATE" — a curated file has
# neither. Also accept any file > ~1500 bytes that doesn't contain the
# template's signature placeholders.
if [ ! -f .specify/memory/constitution.md ]; then
  CONSTITUTION_STATE="missing"
elif grep -q "PROJECT CONSTITUTION TEMPLATE\|<<.*>>" .specify/memory/constitution.md 2>/dev/null; then
  CONSTITUTION_STATE="template"
else
  CONSTITUTION_STATE="curated"
fi
```

If `CONSTITUTION_STATE = template` or `missing`, after completing ALL phases (including validation), tell the user:

> **Next step: set up your project constitution**
>
> Run `/speckit-constitution` now to define your project's principles, workflow rules, and governance. This is a one-time interactive setup that tailors the pipeline to your project.
>
> The constitution template is ready at `.specify/memory/constitution.md` — the command will walk you through filling it in.

If `CONSTITUTION_STATE = curated`, skip the prompt entirely and instead include in the summary report:

> Constitution: preserved (`.specify/memory/constitution.md` already contains curated content — not overwritten).

This should be the LAST thing the user sees before the skill exits — it's their immediate next action when applicable.

---

### Phase 5 — Generate Pipeline Scripts

Create `scripts/` directory and generate all pipeline scripts. Each script reads `.bureau.json` for configuration via a shared helper.

> **Source of truth:** the actual scripts generated in this phase live in
> `~/.claude/skills/bureau-init/templates/scripts/`. The canonical shape for
> `bureau-config.sh` and every pipeline is the file in that directory. The
> inline snippet below is a summary — copy the real files, don't retype them.
>
> `bureau-config.sh` provides `pick_issue`, `move_issue`, `post_comment`,
> `get_issue_branch`, `get_issue_comments`, `get_issue_detail`, `get_issue_state`,
> `add_issue_label`, `linear_query`, `linear_raw`, `alert_telegram`,
> `precondition_linear`, `precondition_claude_auth`, `precondition_clean_worktree`, `agent_enabled`.
> All Linear glue MUST go through these — see Appendix B for the rule.

**Step 5a: Generate the config reader helper**

Create `scripts/bureau-config.sh` — a bash helper that reads `.bureau.json`:

```bash
#!/bin/bash
# bureau-config.sh — reads .bureau.json for pipeline scripts
# Source this file: source "$(dirname "$0")/bureau-config.sh"

BUREAU_CONFIG=""

# Find .bureau.json — check worktree dir, then script's repo dir
_find_config() {
  if [ -f ".bureau.json" ]; then
    BUREAU_CONFIG=".bureau.json"
  elif [ -f "$(cd "$(dirname "$0")/.." && pwd)/.bureau.json" ]; then
    BUREAU_CONFIG="$(cd "$(dirname "$0")/.." && pwd)/.bureau.json"
  else
    echo "ERROR: .bureau.json not found. Run /bureau-init to set up."
    exit 1
  fi
}

_find_config

# Read helpers
bureau_get() { jq -r "$1" "$BUREAU_CONFIG"; }

# Linear config
BUREAU_TEAM_KEY=$(bureau_get '.linear.teams[0].key')
BUREAU_TEAM_ID=$(bureau_get '.linear.teams[0].id')
BUREAU_TEAM_NAME=$(bureau_get '.linear.teams[0].name')

# States
BUREAU_STATE_TRIAGE=$(bureau_get '.linear.teams[0].states.triage')
BUREAU_STATE_SPEC=$(bureau_get '.linear.teams[0].states.spec')
BUREAU_STATE_SPEC_REVIEW=$(bureau_get '.linear.teams[0].states.spec_review')
BUREAU_STATE_DESIGN=$(bureau_get '.linear.teams[0].states.design')
BUREAU_STATE_BUILD=$(bureau_get '.linear.teams[0].states.build')
BUREAU_STATE_BUILD_REVIEW=$(bureau_get '.linear.teams[0].states.build_review')
BUREAU_STATE_DONE=$(bureau_get '.linear.teams[0].states.done')

# Labels
BUREAU_LABEL_LANE2=$(bureau_get '.linear.labels.lane2.id')
BUREAU_LABEL_LANE2_NAME=$(bureau_get '.linear.labels.lane2.name')
BUREAU_LABEL_NEEDS_HUMAN=$(bureau_get '.linear.labels.needs_human.id')
BUREAU_LABEL_NEEDS_UX=$(bureau_get '.linear.labels.needs_ux.id')
BUREAU_LABEL_AI_IMPL=$(bureau_get '.linear.labels.ai_implementable.id')

# Projects filter (empty = all started projects)
BUREAU_PROJECTS=$(bureau_get '.linear.projects // [] | join(",")')

# Agent config
BUREAU_POLL_INTERVAL=$(bureau_get '.agents.poll_interval_minutes // 30')
BUREAU_WORKBENCH_PANES=$(bureau_get '.agents.workbench_panes // 2')
BUREAU_MAX_REVIEW_CYCLES=$(bureau_get '.agents.max_review_cycles // 3')
BUREAU_CODE_REVIEW=$(bureau_get '.agents.code_review // "v2"')

# Repo config
BUREAU_BRANCH_PREFIX=$(bureau_get '.repo.branch_prefix // "feat"')
BUREAU_COMMIT_PREFIX=$(bureau_get '.repo.commit_prefix // ""')
BUREAU_SPECS_DIR=$(bureau_get '.repo.specs_dir // "specs"')

# Projects filter (comma-separated UUIDs; empty = all projects in the team)
BUREAU_PROJECTS=$(bureau_get '.linear.projects // [] | join(",")')

# Helper: query Linear GraphQL
linear_query() {
  curl -s -X POST https://api.linear.app/graphql \
    -H "Content-Type: application/json" \
    -H "Authorization: $API_KEY" \
    -d "{\"query\": \"$1\"}"
}

# Helper: pick next issue from a queue via direct GraphQL (no Claude subprocess, no MCP, no OAuth)
# Usage: pick_issue <state-uuid> <required-label-names-csv> [exclude-label-names-csv]
#
# This REPLACES the legacy "spawn `claude -p` and tell it to query Linear" picker.
# The legacy picker was unreliable in cron because:
#   1. Headless claude subprocesses can't complete an interactive OAuth flow for remote MCP servers
#   2. Linear's MCP OAuth tokens expire after roughly 1 hour
#   3. So the first cron tick after a fresh interactive session worked, then every subsequent tick failed silently
# This direct-GraphQL picker uses only LINEAR_API_KEY, which is long-lived, and is ~20x faster besides.
#
# Dependency awareness (EXP-437): each candidate's inverseRelations are inspected.
# A candidate is skipped if any inbound `blocks` relation points from an issue
# whose state.type is not `completed` or `canceled`. The picker walks the sorted
# list and returns the first unblocked identifier. Deep chains fall out naturally.
# Skipped candidates are logged to stderr with the blocker identifier(s).
pick_issue() {
  local state_id="$1"
  local required_csv="$2"
  local exclude_csv="${3:-}"

  local required_gql
  required_gql=$(printf '%s' "$required_csv" | awk -F',' '
    BEGIN{printf "["}
    {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
    END{printf "]"}
  ')

  # Project filter: every UUID in $BUREAU_PROJECTS → GraphQL
  # `project: { id: { in: [...] } }`. Empty = no clause = all team projects.
  local project_clause=""
  if [ -n "${BUREAU_PROJECTS:-}" ]; then
    local projects_gql
    projects_gql=$(printf '%s' "$BUREAU_PROJECTS" | awk -F',' '
      BEGIN{printf "["}
      {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
      END{printf "]"}
    ')
    [ "$projects_gql" != "[]" ] && project_clause=$(printf ', project: { id: { in: %s } }' "$projects_gql")
  fi

  local query
  query=$(printf '{ issues(filter: { team: { key: { eq: "%s" } }, state: { id: { eq: "%s" } }, labels: { some: { name: { in: %s } } }%s, parent: { null: true } }, orderBy: updatedAt, first: 50) { nodes { identifier priority createdAt labels { nodes { name } } inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } } } } }' \
    "$BUREAU_TEAM_KEY" "$state_id" "$required_gql" "$project_clause")

  local payload
  payload=$(jq -n --arg q "$query" '{query: $q}')

  local exclude_json
  exclude_json=$(printf '%s' "$exclude_csv" | awk -F',' '
    BEGIN{printf "["}
    {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
    END{printf "]"}
  ')

  # Sorted candidate list, one per line: <identifier>\t<open-blockers-csv>
  # The blockers column is empty when nothing blocks the candidate.
  local candidates
  candidates=$(curl -s -X POST https://api.linear.app/graphql \
    -H "Content-Type: application/json" \
    -H "Authorization: ${API_KEY:-$LINEAR_API_KEY}" \
    -d "$payload" \
  | jq -r --argjson excl "$exclude_json" '
    (.data.issues.nodes // [])
    | map(select(
        ([(.labels.nodes // [])[].name] | map(select(. as $n | $excl | index($n))) | length) == 0
      ))
    | map(. + {_pri: (if .priority == 0 then 5 else .priority end)})
    | sort_by(._pri, .createdAt)
    | .[]
    | [ .identifier,
        ([(.inverseRelations.nodes // [])[]
          | select(.type == "blocks")
          | .issue
          | select(.state.type != "completed" and .state.type != "canceled")
          | .identifier
         ] | join(","))
      ]
    | @tsv
  ')

  # Walk in priority order; log every blocked skip and emit the first that is unblocked.
  while IFS=$'\t' read -r ident blockers; do
    [ -z "$ident" ] && continue
    if [ -n "$blockers" ]; then
      echo "pick_issue: skip $ident (open blockers: $blockers)" >&2
      continue
    fi
    printf '%s' "$ident"
    return 0
  done <<<"$candidates"
}
```

**Step 5b: Generate pipeline scripts**

Generate these scripts, each sourcing `bureau-config.sh` for all IDs and config. The scripts should be functionally identical to the ones described in Appendix B below, but with all hardcoded values replaced by `$BUREAU_*` variables.

Scripts to generate:
1. `scripts/bureau-config.sh` — config reader (from step 5a)
2. `scripts/queue-loop.sh` — orchestrator loop
3. `scripts/spec-pipeline.sh` — Triage → Spec Review
4. `scripts/spec-review-pipeline.sh` — Spec Review → Build/Design
5. `scripts/ux-pipeline.sh` — Design → Build (only if ux agent enabled)
6. `scripts/copy-pipeline.sh` — Copy → Build (only if copy agent enabled; no-op at runtime if states.copy absent)
7. `scripts/implement-pipeline.sh` — Build → QA (if qa enabled) or Build Review
8. `scripts/qa-pipeline.sh` — QA → Build Review (only if qa agent enabled; no-op at runtime if states.qa absent)
9. `scripts/code-review-pipeline.sh` — Build Review → Merge|Done (Done is the path when merge agent disabled; Merge when enabled)
9a. `scripts/merge-pipeline.sh` — Merge → Done (only if merge agent enabled; gated PR merger; no-op at runtime if states.merge absent)
9b. `scripts/rebase-pipeline.sh` — DIRTY bureau-only PR → Build Review (only if rebase agent enabled; force-pushes; no-op at runtime if states.merge absent)
10. `scripts/start-agents.sh` — tmux launcher
11. `scripts/start-bureau-v2.sh` — enhanced tmux launcher with dashboard + workbench
12. `scripts/bureau-status.sh` — live dashboard
13. `scripts/grab-issue.sh` — manual issue pickup
14. `scripts/complete-issue.sh` — manual issue completion
15. `scripts/crosscheck-specs.sh` — spec vs PR file conflict checker

Make all scripts executable: `chmod +x scripts/*.sh`

**Critical changes from the hardcoded originals:**

In ALL pipeline scripts, replace:
- `Team: Experiments` → `Team: $BUREAU_TEAM_NAME`
- `Label: lane-2` → `Label: $BUREAU_LABEL_LANE2_NAME`
- Hardcoded state UUIDs → `$BUREAU_STATE_*` variables
- Hardcoded label UUIDs → `$BUREAU_LABEL_*` variables
- `exp/` branch prefix → `$BUREAU_BRANCH_PREFIX/`
- Fixed team key parsing → use `$BUREAU_TEAM_KEY`

In `queue-loop.sh`, read enabled agents from `.bureau.json`:
- Only create windows/modes for agents where the config value is `true` or a version string
- The `all` mode should only run enabled agents

In `start-bureau-v2.sh` and `start-agents.sh`:
- Only create tmux windows for enabled agents
- Read `poll_interval_minutes` and `workbench_panes` from config

In `bureau-status.sh`:
- Only show status for enabled agents
- Read agent list from config

In `grab-issue.sh` and `complete-issue.sh`:
- Replace hardcoded state UUIDs with values from `bureau-config.sh`

---

### Phase 6 — CLAUDE.md & Environment Setup

**Step 6a: Update or create CLAUDE.md**

If CLAUDE.md exists, append the workflow section (if not already present — check for "## Workflow" or "bureau-init" marker).

If CLAUDE.md doesn't exist, create one with:

```markdown
# CLAUDE.md — Agent Instructions

## Workflow

### Linear Integration
- API key: `$LINEAR_API_KEY` (from .env)
- Team: {team_name} ({team_key})
- Pick up: issues labeled `{lane2_label_name}` in `Triage` state
- On start: move to `Build`
- On complete: move to `Review`

### Spec-Driven Development
This repo uses spec-kit. Speckit installs as native Claude Code skills under `.claude/skills/speckit-*/SKILL.md` — invoke them as `/speckit-specify`, `/speckit-plan`, etc.
Minimum flow: `/speckit-specify` -> `/speckit-plan` -> `/speckit-tasks` -> `/speckit-implement`
Specs are stored in `specs/`.

### Multi-Agent Pipeline
Pipeline config: `.bureau.json`
Scripts: `scripts/`
Start agents: `./scripts/start-bureau-v2.sh`

### Memory Loop (logs → LESSONS.md)
`queue-loop.sh` appends one structured event per stage run to `logs/events.jsonl` (gitignored — local-only).
Run `/bureau-learnings` weekly to draft `LESSONS.md` from those events plus Linear comments. The draft is never auto-committed — review the diff, edit freely, then `git commit LESSONS.md`. The spec and code-review pipelines selectively read `LESSONS.md` back in as advisory context. To dismiss a finding, delete its bullet; if the pattern persists, the next `/bureau-learnings` will re-propose it.

## Constraints
- Never commit directly to main — always feature branches
- If working from a Linear issue, reference the ID in commits

<!-- bureau-init managed -->
```

**When any of `.agents.use_goal_loop`, `.agents.headroom_wrap`, or `.agents.caveman_level` is enabled in `.bureau.json`, also append the following block immediately before the `<!-- bureau-init managed -->` marker.** Render only the sub-sections whose flag is on — keep the generated CLAUDE.md uncluttered when an adopter hasn't opted in. Re-emit on `--update` / `--resync-scripts` if the flags changed.

```markdown
## Token-efficiency mode

This repo runs the bureau pipeline with one or more efficiency layers enabled. They are transparent to most workflows but change a few behaviours you'll see — read the relevant subsections before assuming the pipeline works like vanilla bureau.

<!-- if .agents.use_goal_loop -->
### `/goal`-driven implementation

implement-pipeline invokes you via `claude -p "/goal CONDITION"` instead of a per-iter bash loop. Haiku evaluates the goal condition after every turn; you don't see the evaluator's reasoning, but if the condition isn't met your next turn starts automatically. Practical implications:

- End every turn with the fenced JSON status block (status / tasks_done / fixed_review_items / notes). The bash downstream still parses it for the PR / state-move flow.
- Do NOT emit `status=COMPLETE` without commits to back it. A branch-wide commit count gate runs after `/goal` returns and will flip lying-COMPLETE to STUCK.
- The single-strike stuck detector that historically misfired on legitimate COMPLETE / mid-flight PARTIAL is gone on this path — Haiku does the same check structurally.
<!-- /if -->

<!-- if .agents.headroom_wrap -->
### Headroom-compressed input

`claude_cmd_for_stage` prefixes every Claude invocation with `headroom wrap`, so user-prompt tool outputs and file reads you receive may be summarized. Practical implications:

- If a summary is missing a specific line range, function body, or stack frame you need, call the `headroom_retrieve` MCP tool to fetch the original. Do not hallucinate — ask for the original.
- The original is always retrievable; the compression layer is reversible (CCR). Cost of a retrieve is one tool call.
- File you wrote yourself this turn is never compressed before you re-read it.
<!-- /if -->

<!-- if .agents.caveman_level != off -->
### Caveman output style

Caveman is scoped to **review-prose stages only**: code-review comments, status comments, PR review summaries. Commit messages and PR titles/bodies stay in normal register because human readers need them. Practical implications:

- When you're invoked for a review or status-comment task and detect the prompt prefix `/caveman <level>`, prefer telegraphic sentences. Drop ceremony ("I'd be happy to", "Let me take a look at", "The reason this is happening is").
- Code, file paths, function names, error strings stay byte-exact regardless of level.
- Levels: `lite` (drop filler), `full` (default caveman), `ultra` (telegraphic), `wenyan` (classical Chinese, even shorter — only enable in repos with Chinese-reading reviewers).
<!-- /if -->
```

**Step 6b: Create .env.example**

Generate `.env.example` with required variables:

```
# Required for pipeline
LINEAR_API_KEY=lin_api_...

# Optional — add project-specific keys below
```

If `.env` doesn't exist, create it from the example and remind the user to add their key.

**Step 6c: Add to .gitignore**

Ensure these are in `.gitignore`:
```
.bureau.json
logs/
.worktrees/
.env
.env.local
```

**Step 6d: Scaffold the CI workflow (offer, never overwrite)**

The bureau gates on **real CI** (`merge-pipeline.sh` enforces green-CI independently of GitHub's `mergeStateStatus`), so an adopting repo should have a CI workflow. Bureau ships a scaffold at `~/.claude/skills/bureau-init/templates/.github/workflows/ci.yml` — defaults to `runs-on: ubuntu-latest` (GitHub-hosted; safe for public repos). Self-hosted runners are a documented opt-in only for private, non-production hosts.

Classify the adopter's `./.github/workflows/ci.yml`, then act — model this exactly on the Step 5 "missing → install / drifted → diff+confirm" pattern:

- **Missing** (`./.github/workflows/ci.yml` does not exist) → show the first ~15 lines of the template, then ask:
  **"Install a CI workflow? Defaults to `runs-on: ubuntu-latest` (GitHub-hosted, safe for public repos). Swap to your own `[self-hosted, ...]` labels only if the runner is private + non-production — self-hosted runners on public repos are a fork-PR-RCE class. (Y/n)"** — default **Yes**.
  On yes / `<enter>`:
  ```bash
  mkdir -p ./.github/workflows
  cp ~/.claude/skills/bureau-init/templates/.github/workflows/ci.yml ./.github/workflows/ci.yml
  ```
  On `n` / `no` → skip and note it in the summary.
- **Identical** (`cmp -s` matches the template) → nothing to do.
- **Drifted** (exists, differs) → **never overwrite silently.** Show `diff -u ./.github/workflows/ci.yml ~/.claude/skills/bureau-init/templates/.github/workflows/ci.yml | head -40`, ask **"Overwrite `.github/workflows/ci.yml`? (y/N)"** — default **No**. Most repos will have customized the test command, so default to keeping theirs.

After installing, remind the operator: the `- run: bash tests/run.sh` step is a placeholder — replace it with the repo's real build/test command (the template carries commented Rust and bureau-init examples), and if they have no self-hosted runner they must swap `runs-on` to `ubuntu-latest`.

**Step 6e: Token-efficiency layer installs (conditional)**

Read each flag from the `.bureau.json` just written in Phase 3. For each one that's enabled, run the install side-effect ONCE — these are idempotent; safe to re-run on `--update` / `--resync-scripts` paths too.

```bash
HEADROOM_WRAP=$(jq -r '.agents.headroom_wrap // false' .bureau.json)
CAVEMAN_LEVEL=$(jq -r '.agents.caveman_level // "off"' .bureau.json)
USE_GOAL_LOOP=$(jq -r '.agents.use_goal_loop // false' .bureau.json)
```

- **`headroom_wrap: true`** → probe `headroom --version`. If absent, surface a one-line install hint (`pip install "headroom-ai[all]"`) and continue — don't block the rest of setup. The flag is read live by `claude_cmd_for_stage` (see `bureau-config.sh::headroom_wrap_enabled`), so an operator who installs headroom later doesn't need to re-run /bureau-init.
- **`caveman_level: lite|full|ultra|wenyan`** → install the skills bundle once:
  ```bash
  npx skills@latest add JuliusBrussee/skills
  ```
  After install, run `/caveman-compress CLAUDE.md` to shrink the just-generated CLAUDE.md from Step 6a. Caveman is scoped to per-stage review-prose only (the per-stage prompts in `code-review-pipeline.sh` pick the level up from `caveman_level()` in `bureau-config.sh`); it does NOT touch commit messages or PR bodies — those stay readable. Skip entirely on `caveman_level: off`.
- **`use_goal_loop: true`** → no install side-effect; the flag is read live by `bureau-config.sh::use_goal_loop_enabled` on every implement-pipeline tick. Just verify the local Claude Code version is ≥ 2.1.139 (`claude --version`); warn if older — `/goal` is a no-op on pre-2.1.139 builds.

Concept docs for what these layers do and how they compose: `docs/token-efficiency.md`. Operator-facing flag schema: `docs/configuration.md`.

---

### Phase 7 — Validation

Run these checks and report results:

```bash
# 1. Config is valid JSON
jq empty .bureau.json

# 2. Linear API works
curl -s -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: $LINEAR_API_KEY" \
  -d '{"query": "{ viewer { id name } }"}' | jq -r '.data.viewer.name'

# 3. Team and states resolve
# Query team by ID, verify states exist

# 4. Scripts are executable
ls -la scripts/*.sh | head -5

# 5. Git worktrees can be created
git worktree list

# 6. Branch protection on default branch (advisory — warn if missing)
gh api "repos/$(gh repo view --json nameWithOwner --jq .nameWithOwner)/branches/main/protection" 2>/dev/null \
  | jq -r 'if .required_status_checks.strict then "✓ branch protection strict" else "⚠ branch protection NOT strict" end' \
  || echo "⚠ no branch protection on main (recommended for merge agent — see README §Branch protection)"
```

If check 6 reports a warning AND `agents.merge: true` in `.bureau.json`, append to the summary output:

> ⚠ **Branch protection not configured on `main`.** The bureau's merge gates
> enforce green-CI and up-to-date-base independently, but configuring GitHub
> branch protection (strict status checks + required up-to-date) is the
> belt-and-suspenders backstop. See README §"Branch protection (strongly
> recommended)" for the `gh api` command.

Do NOT auto-create the protection rule — easy to misconfigure and lock the operator out. Surface the warning and the command; the operator decides.

**Print summary:**

```
Bureau Pipeline Initialized

  Repo:      {repo_name}
  Team:      {team_name} ({team_key})
  Labels:    {lane2_name} (eligible), {needs_human_name} (escalation)
  Agents:    {list of enabled agents}
  Interval:  {N}m
  Speckit:   {initialized/updated/existing}

  Config:    .bureau.json
  Scripts:   scripts/
  Specs:     specs/
  Speckit:   .claude/skills/speckit-*/SKILL.md (v0.7.5, via specify CLI)
  Commands:  .claude/commands/{check-linear-queue,linear-to-spec,…}.md (bureau-init)

Next steps:
  1. {if CONSTITUTION_STATE != curated} Run /speckit-constitution to set project governance
     {else}                            Constitution preserved at .specify/memory/constitution.md (already curated)
  2. Start the pipeline: ./scripts/start-bureau-v2.sh
  3. Attach to tmux: tmux attach -t bureau-v2-<repo-slug>
  4. Create issues in Linear with the '{lane2_name}' label
  5. After a week of activity, run /bureau-learnings to draft LESSONS.md from logs/events.jsonl
```

Render exactly one of the two step-1 branches (drop the `{if}`/`{else}` markers from the actual output) — the `CONSTITUTION_STATE` variable is set in Phase 4f's heuristic. The intent: don't badger users with curated constitutions to "run /speckit-constitution" when their content already exists.

---

## Appendix A — Speckit Templates

bureau-init no longer ships speckit templates. Phase 4 delegates to `specify init --here --integration claude --force` (pinned in `BUREAU_SPECKIT_VERSION`), which writes the canonical `.specify/templates/*.md`, `.specify/scripts/bash/*.sh`, and `.claude/skills/speckit-*/SKILL.md` files for the pinned spec-kit release.

Refer to upstream for the current schema: <https://github.com/github/spec-kit/tree/main/templates>.

When bumping `BUREAU_SPECKIT_VERSION`, run a spike (`specify init --here --integration claude --no-git` in a throwaway dir) and diff against the previous pin. Watch for renamed slash commands, layout changes, or removed templates — last big break was v0.4.5 (commands → native skills, dot → hyphen in slash names).

---

## Appendix B — Pipeline Script Patterns

Each pipeline script follows this structure:

```bash
#!/bin/bash
set -euo pipefail

# Prevent nested session error
unset CLAUDECODE 2>/dev/null || true

# Source config
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

# Source .env
if [ -f .env ]; then
  source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then
  source "$SCRIPT_REPO/.env"
fi

CLAUDE="claude -p --print --dangerously-skip-permissions"
API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

# ... pipeline-specific logic using $BUREAU_* variables ...
```

**Queue picking: use `pipeline_pick_next`, not `claude -p`.** Every pipeline script that chooses the next issue from a state queue MUST call `pipeline_pick_next` from `bureau-config.sh`, which dispatches to `pick_issue` via the central registry. Do NOT generate the legacy pattern of spawning `$CLAUDE "You are a queue picker..."` — that pattern is unreliable in cron because headless Claude subprocesses can't complete OAuth for remote MCPs, and Linear's MCP OAuth tokens expire after ~1 hour. The first cron tick works, every subsequent tick fails silently with "No qualifying issues found."

Canonical picker block for each pipeline:

```bash
if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next <stage> issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")

  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi
```

Stage → registry mapping (defined once in `bureau-config.sh:pipeline_picker_args`; both `queue-loop.sh:preselect_issue` and each pipeline's own picker call read from there). Add a row to that function when adding a new pipeline; the table below is documentation, not the source of truth.

| Pipeline | State var | Required labels | Exclude labels |
|---|---|---|---|
| spec-pipeline.sh | `$BUREAU_STATE_TRIAGE` | `"$BUREAU_LABEL_LANE2_NAME"` | `""` |
| spec-review-pipeline.sh | `$BUREAU_STATE_SPEC_REVIEW` | `"$BUREAU_LABEL_LANE2_NAME"` | `""` |
| ux-pipeline.sh | `$BUREAU_STATE_DESIGN` | `"needs-ux,$BUREAU_LABEL_LANE2_NAME"` | `""` |
| copy-pipeline.sh | `$BUREAU_STATE_COPY` (opt-in) | `"$BUREAU_LABEL_NEEDS_COPY_NAME,$BUREAU_LABEL_LANE2_NAME"` | `"needs-human"` |
| implement-pipeline.sh | `$BUREAU_STATE_BUILD` | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human"` |
| qa-pipeline.sh | `$BUREAU_STATE_QA` (opt-in) | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human"` |
| code-review-pipeline.sh | `$BUREAU_STATE_BUILD_REVIEW` | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human"` |
| merge-pipeline.sh | `$BUREAU_STATE_MERGE` (opt-in) | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human,blocked,wip"` |
| rebase-pipeline.sh | `$BUREAU_STATE_MERGE` (opt-in) | `"$BUREAU_LABEL_LANE2_NAME,ai-implementable"` | `"needs-human,blocked,wip"` |

**Opt-in pipelines** (`copy`, `qa`) exit 2 (queue-empty) when their state UUID or required label name is empty in `.bureau.json`. This means a repo that doesn't configure them never sees them run — the pipeline script itself gates on the config, not just `agent_enabled` in `queue-loop.sh`. Both gates are defensive: the config gate catches the case where an operator enables the agent in `agents.qa: true` but forgot to configure the state.

`pick_issue` uses label **names** (not UUIDs) in its GraphQL filter, so custom labels like `ai-implementable`, `needs-human`, `needs-ux` work even when `.bureau.json` doesn't have their UUIDs captured. It reads the team/project/state config from the `$BUREAU_*` variables and does one direct GraphQL POST using `LINEAR_API_KEY` — no MCP, no OAuth, no Claude subprocess, no token expiry.

### All Linear glue uses direct GraphQL, never `$CLAUDE` (EXP-412)

The same reasoning that kills `$CLAUDE` for queue picking kills it for every other Linear interaction. Headless `claude -p` subprocesses cannot refresh Linear's OAuth tokens, and a failing MCP call returns empty output that bash's `if [ -z "$X" ]` guards mistake for "no work". Every Linear glue operation must go through a helper in `bureau-config.sh`:

| Helper | Replaces |
|---|---|
| `move_issue <issue> <state-uuid>` | `$CLAUDE "Move Linear issue ... to state ..."` |
| `post_comment <issue> <body>` | `$CLAUDE "Post a comment ..."` |
| `get_issue_branch <issue>` | `$CLAUDE "Find the spec/implementation branch ..."` |
| `get_issue_comments <issue>` | `$CLAUDE "Check comments for code review feedback ..."` |
| `get_issue_detail <issue>` | per-script `linear_query` + `jq` for title/description/project |
| `get_issue_state <issue>` | per-script `linear_query` for the state-name check after picking |
| `add_issue_label <issue> <name>` | `$CLAUDE "Add label X to issue Y"` |

`$CLAUDE` is reserved for **creative work only**: spec-pipeline's optional pre-spec research call (WebFetch/WebSearch against current API docs, label-gated on `needs-research`) and its `speckit-specify`/`speckit-plan`/`speckit-tasks` invocations (read via the `.claude/skills/speckit-*/SKILL.md` files), implement-pipeline's main implementation call, code-review-pipeline's specialist review calls, spec-review-pipeline's validation prose. A rule of thumb: if the prompt is shorter than the response, it's glue — use a helper. If the prompt is longer and describes a task, Claude does the work.

### Deterministic branch discovery via bureau-branch marker (EXP-413)

Linear's auto-generated `branchName` field is derived from the issue title and does **not** match the sequential spec-number branches the spec pipeline creates (`001-automated-tests`, `004-graphify-integration`, etc.). Every pre-EXP-413 "find the branch" lookup was a silent mismatch.

The fix is a marker comment that the **spec pipeline** posts along with the spec digest:

```markdown
<!-- bureau-branch: 001-automated-tests -->
**Spec Artifacts — EXP-404**
...
```

The marker renders invisibly in Linear (HTML comment) but is parsed by `get_issue_branch()` on the way back. Every downstream pipeline (spec-review, ux, implement, code-review) resolves the branch from this marker, not from `branchName`. When the marker is missing or points at a branch that no longer exists, pipelines **fail loud**: post an explanation comment, route the issue back to Triage, and exit with a distinct non-zero code. No silent fresh-from-main fallback, no 1300-line implementation runs on the wrong base.

### Merge origin/main before testing or reviewing (EXP-484)

A spec branch is cut from `origin/main` once, but the pipeline that tests, builds, or reviews it may run many ticks (and many merges to main) later. Without merging current main into the branch, the pipeline operates on a stale base:

- **qa-pipeline.sh** runs the test suite against an old `main` — fixes that depend on a recent migration false-fail.
- **implement-pipeline.sh** runs Claude on possibly-removed APIs and may re-introduce patterns that were just deleted.
- **code-review-pipeline.sh** computes `git diff origin/main...HEAD` against an outdated tip, so the diff is polluted with phantom-revert hunks (commits that landed on main after the branch was cut), and `npm run build` compiles against a stale base.

Every pipeline that compiles or runs tests therefore calls `merge_origin_main_or_abort <issue> <stage-label>` from `bureau-config.sh` immediately after checking out the branch. The helper:

- No-ops when the branch is already up to date with `origin/main` (`git merge-base --is-ancestor`).
- Otherwise runs `git merge --no-ff --no-edit origin/main`. Returns 0 on success.
- On conflict, aborts the merge, posts an explanatory comment to the issue, and returns 1. The caller decides routing — all three pipelines exit 17 (`rebase-needed`, distinct from 12 `no-branch` so the Telegram alert classifies correctly): qa and code-review route back to Build; implement labels `needs-human` and stays in Build (the picker excludes `needs-human`, so the issue is parked until a human rebases).

`spec-review-pipeline.sh` doesn't call this helper — it doesn't compile or run tests, only validates spec prose.

### Per-stage model override (EXP-490)

Each pipeline can run on a different model — different stages have meaningfully different requirements (spec/plan want strongest reasoning; implementation wants strongest coding; validators want a *different perspective* than workers; mechanical stages can use Haiku).

The mechanism is a single helper in `bureau-config.sh`:

```bash
claude_cmd_for_stage <stage>
```

Resolution order: `agents.<stage>.model` → `agents.model` → empty (no `--model` flag, falling through to whatever the `claude` CLI default is). Each pipeline sets `CLAUDE=$(claude_cmd_for_stage "<stage>")` instead of hardcoding the literal — so an unconfigured repo behaves identically to before.

Configuration in `.bureau.json`:

```json
{
  "agents": {
    "model": "claude-sonnet-4-6",
    "spec":         { "model": "claude-opus-4-7" },
    "implement":    { "model": "claude-opus-4-7" },
    "code_review":  { "model": "claude-haiku-4-5-20251001" },
    "qa":           { "model": "claude-haiku-4-5-20251001" }
  }
}
```

Stage names mirror the agent gates: `spec`, `spec_review`, `ux`, `copy`, `implement`, `qa`, `code_review`, `merge`.

**Provider mixing constraint** (per Factory's prior art): reasoning traces are encrypted differently per provider. Anthropic models with reasoning enabled require other Anthropic models in the same context. OpenAI models can only pair with other OpenAI models. Bureau is fine here — every pipeline run is a fresh `claude -p` subprocess with no shared reasoning context, so cross-provider stage assignment works as long as each stage's full conversation stays within its provider.

**Validator independence.** The strongest reason to mix models is that a validator using a *different model* from the worker catches what the worker missed. Bureau's code-review pipeline already runs 3 specialists in parallel for orthogonal *perspectives*; setting `agents.code_review.model` to a different model than `agents.implement.model` adds a *model-level* difference on top of the role-level one.

### Auto-restart supervisor for queue-loop (EXP-382)

`queue-loop.sh` runs an infinite while-true loop — any exit means the process was killed (OOM, terminal disconnect, unhandled bash error, panicked subprocess). Without a supervisor the dead tmux pane stays dead until a human notices.

**`scripts/queue-loop-supervised.sh`** is a drop-in wrapper that `start-bureau-v2.sh` uses in place of `queue-loop.sh`. It:

1. **Restarts on crash** with exponential backoff: 10 s → 30 s → 60 s → 300 s (capped at 5 min).
2. **Resets the crash counter** after `BUREAU_SUPERVISOR_STABILITY_WINDOW` seconds of clean runtime (default 1 h). A long-lived agent that crashes once doesn't permanently cap its restart speed.
3. **Gives up** after `BUREAU_SUPERVISOR_MAX_CRASHES` consecutive crashes (default 5) and fires a Telegram alert with the tail of the queue log before exiting 1.
4. **Forwards SIGINT / SIGTERM** to the child so `Ctrl+C` in the tmux pane stops everything cleanly without triggering the restart logic.

Logs to `logs/supervisor-<mode>.log`. The Telegram alert uses the standard `alert_telegram` helper, so it's a no-op when credentials are unset.

Configuration in `.bureau.json`:

```json
{
  "supervisor": {
    "max_crashes": 5,
    "stability_window": 3600
  }
}
```

`start-bureau-v2.sh`'s `add_agent_window` function calls `./scripts/queue-loop-supervised.sh $mode $INTERVAL` instead of `queue-loop.sh` directly. To opt out of supervision (e.g. for debugging), call `queue-loop.sh` directly from a bench pane.

### Drain before refilling — single-flight + stage-priority (EXP-491)

In active repos with many concurrent feature tickets, branches accumulate divergence faster than the pipeline can drain them. Cause: every cron tick, multiple branches re-run `merge_origin_main_or_abort` against an `origin/main` that advanced since last tick. Trivial conflicts auto-resolve (per the resolver added 2026-05-08), but real conflicts pile up and the loop spins. Two complementary knobs:

**A. Stage-priority sort in `queue-loop.sh`'s `all` mode.** Fan-out order is *reversed* from state-machine sequence — `merge`/`rebase` first, `spec` last. When multiple stages have pickable issues, attention goes to the ones closest to Done. Drains before refilling. Zero behaviour change for the default deployment (each agent runs in its own tmux window with its own `queue-loop.sh <mode>` — independent, so cross-stage ordering only affects the single-process `all` mode).

**B. `BUREAU_MAX_CONCURRENT_ISSUES` cap.** Repo-wide cap on how many *distinct* issues the bureau works on simultaneously. Default `0` = unlimited (current behaviour). Set to `1` for single-flight mode (drain one issue end-to-end before another enters Spec). Implementation:

- `count_in_flight_issues()` in `bureau-config.sh` queries Linear for issues in any state between Spec (inclusive) and Done (exclusive). Issues with parking labels (`needs-human`, `blocked`, `wip`) are excluded so a stalled issue doesn't deadlock the cap.
- `spec-pipeline.sh` checks the count immediately after preconditions; exits 2 (queue-empty) if at cap. Only spec gates — downstream stages keep running on already-in-flight issues, so cap=1 still drains the current ticket.
- Fail-open on Linear errors: `count_in_flight_issues` returns "0" on any query failure, so a network blip doesn't block work.

Configuration in `.bureau.json`:

```json
{
  "agents": {
    "max_concurrent_issues": 1
  }
}
```

The cap is a Linear query, not a coordinator process — no new daemon, no shared state file, no race conditions.

### Fail-loud observability (EXP-414)

Every pipeline script:

1. Starts with `set -euo pipefail`.
2. Calls `precondition_linear` before any Claude work, exiting 10 if `LINEAR_API_KEY` is missing or invalid (one-line `viewer { id }` query).
3. Uses distinct exit codes for each failure class so `queue-loop.sh` can alert correctly:

| Exit | Class | Meaning |
|---|---|---|
| 0 | ok | completed successfully |
| 2 | queue-empty | nothing to pick — normal |
| 10 | linear-down | `LINEAR_API_KEY` missing/invalid |
| 11 | worktree-dirty | uncommitted changes in worktree |
| 12 | no-branch | bureau-branch marker missing or points at a non-existent branch |
| 13 | no-tasks | tasks.md expected but missing |
| 14 | build-failed | build precondition failed |
| 15 | no-pr | PR expected but not found |
| 16 | claude-unauth | `claude` CLI not logged in |

`queue-loop.sh` captures the exit code, maps it to a class, and calls `alert_telegram` (throttled to max 1 alert per issue/class/hour via `/tmp/bureau-alerts.log`). The alerter is a best-effort no-op when `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALERT_CHAT_ID` are unset, so dev environments don't break.

### Spec pipeline failure recovery (EXP-416)

`spec-pipeline.sh` moves the issue from Triage → Spec **before** running speckit phases. To prevent stranding issues in Spec on failure:

1. **Preconditions before state mutation**: `precondition_linear` (exit 10) and `precondition_claude_auth` (exit 16) run before any `move_issue` call. `precondition_claude_auth` probes the `claude` CLI with a trivial prompt to detect auth failures early — if the CLI is not logged in, the pipeline exits 16 without touching Linear state.
2. **Recovery trap**: An EXIT trap (`_spec_recovery`) is installed immediately after the Triage→Spec move. On any non-zero exit during speckit phases (specify, plan, tasks, crosscheck, push), the trap routes the issue back to Triage with an explanatory comment and fires a Telegram alert.
3. **Trap cleared on success**: The trap is cleared (`trap - EXIT`) just before the final `move_issue → Spec Review`, so the success path doesn't trigger recovery.

This pattern ensures issues are never stranded in Spec with zero artifacts. The same recovery approach can be extended to other pipelines.

### Pre-spec research (optional, label-gated, best-effort)

For issues that integrate with external APIs/SDKs or pin to specific library versions, the spec agent's stale training data is a known failure mode — it confidently invents method signatures and config keys that don't exist. `spec-pipeline.sh` solves this with an **optional** research pass that runs immediately before `speckit-specify`, gated by:

1. The Linear label `needs-research` is present on the issue, **and**
2. `.agents.research` is configured in `.bureau.json` (either `true` or `{"model": "<id>"}` — typically a cheaper model like Haiku since the work is reading docs, not designing code).

When both conditions hold, `$CLAUDE` is invoked with WebFetch/WebSearch enabled and instructed to compile a markdown digest of the relevant APIs (current versions, endpoints/method signatures, recent breaking changes, gotchas). The output must start with the marker `<!-- bureau-research: <api-list> -->`. On success the digest is posted to Linear as a comment (visible to spec-review for traceability), injected into the `speckit-specify` prompt as `$RESEARCH_CONTEXT` (the spec agent is told to treat it as authoritative for API shapes), and the `needs-research` label is stripped.

The whole stage is **best-effort**: a failed research call (non-zero exit, missing marker, network down) is swallowed with `|| true` / `|| echo ""` and the pipeline falls through to specify without research context. The exit-code protocol (0/2/10–16) is untouched — no new class. The label is only stripped *after* a successful `post_comment`, so a crashed run leaves the label in place and the next pick retries naturally. Default is **off** (no `.agents.research` entry → `agent_enabled` returns false), so existing deployments are unchanged until opted in.

### Worktree reset between picks (EXP-415 Part A)

`queue-loop.sh` resets each worktree to a known state **before** every pipeline call. Spec worktrees reset to `origin/main`; spec-review, implement, ux, qa, copy, and code-review worktrees check out the issue's spec branch (resolved via `get_issue_branch`). Every reset includes `clean -fdx` to strip any carryover. The invariant: at the start of a pipeline, the worktree HEAD is exactly where it should be for that pipeline and that issue, not where the previous issue left it.

### JSON-block parsing for stage outputs

Every pipeline whose output the shell parses — `spec-review`, `implement`, `qa`, `copy`, `code-review` — now instructs Claude to emit a trailing fenced `` ```json `` block with the structured fields the shell needs (verdict, status, ui_work_needed, strings_changed, etc.). The shell side uses `parse_claude_json` from `bureau-config.sh`, which `awk`-extracts the last `json` block and pipes it to `jq -r`. **Never** parse these outputs with `grep` / `sed` on the prose — the merger format drifts as the model varies (this drift is why commit `2397837` added the `VERDICT=${VERDICT:-BLOCK}` fallback in code-review). A regex parse that silently lands on "empty" and routes the happy-path without anyone noticing is worse than a loud parse failure.

When adding a new Claude call whose output needs programmatic consumption:
1. End the prompt with a fenced `` ```json ``` block showing the exact schema as a template (zeros / empty strings / empty arrays for defaults).
2. In the calling shell, use `parse_claude_json "$OUTPUT" '.field'`.
3. Add an explicit fallback for empty parses — the safer choice (FAIL for reviewers, BLOCK for verdicts, NEEDS_HUMAN for executors) so a malformed response doesn't auto-advance a state.

### New agents: QA (mechanical) and Copy (opt-in)

- **`qa-pipeline.sh`** slots between Build and Build Review. It's a *mechanical* executor — runs `npm test` / `cargo test` / `pytest` etc. before any Claude call, only engages Claude on test failure or missing coverage. Its job is distinct from code-review: QA confirms the test oracle passes; code-review judges the code's correctness in paths tests don't cover. Running QA first also keeps code-review from burning tokens on a PR whose build is red. Opt-in via `states.qa` + `agents.qa: true`. When QA is configured, `implement-pipeline.sh` routes issues to QA first instead of directly to Build Review.

- **`copy-pipeline.sh`** slots between Design and Build (or wherever you route `needs-copy` issues). It's opt-in at both the state level (`states.copy`) and the label level (`labels.needs_copy.name`). It reads an optional voice guide (`repo.copy_voice_file`) and polishes user-facing strings — button labels, error messages, empty states. It leaves tests, logs, and code comments alone.

Both pipelines use the same JSON-block output convention and the same exit-code vocabulary as the other stages (10 / 11 / 12 / 16 for preconditions, 2 for queue-empty, 0 for success).

### New agents: Merge (gated) and Rebase (opt-in, force-pushes)

Reviewed PRs accumulate when nothing closes the loop: mergeable-and-approved PRs sit waiting for someone to click Merge, branches go DIRTY when main moves, and the 3-cycle loop-breaker escalates to needs-human without followup. The `merge` agent gates and merges; the `rebase` agent unsticks DIRTY bureau-only branches.

- **`merge-pipeline.sh`** picks issues from `BUREAU_STATE_MERGE` and merges only when EVERY gate passes:
  1. PR `state == OPEN`
  2. `mergeStateStatus == CLEAN` (GitHub heuristic — async-cached, lax when branch protection isn't strict)
  3. **`pr_ci_is_green` (bureau-enforced, NRSR)**: every check-run AND legacy status context on the PR's current head SHA is `completed` and `success|skipped|neutral`. Pending/in-progress is rejected. Independent of `mergeStateStatus` because CLEAN passes when no required-checks rule is configured. Toggle: `.agents.merge_require_green_ci` (default true).
  4. **`pr_base_is_current` (bureau-enforced, NRSR)**: PR's `baseRefOid` == `origin/<baseRef>`'s HEAD. Catches the stale-base race where mergeStateStatus's async cache still reads CLEAN after main moved. Toggle: `.agents.merge_require_up_to_date` (default true).
  5. Latest PR comment matching `## Code Review v2 — ` carries `**Verdict**: APPROVE` (or `AUTO_APPROVE`)
  6. No `needs-human` / `blocked` / `wip` label on the PR
  7. Zero unresolved review threads (GraphQL `pullRequest.reviewThreads`)

  The entire gate set is **re-evaluated just-in-time** (a second `evaluate_merge_gates` call) immediately before `gh pr merge`. If anything regressed between the initial pass and the merge call (most importantly: gate 4 because a prior tick may have merged a different PR that advanced main), the pipeline aborts with `exit 0` and the next tick re-evaluates. This closes the window where mergeStateStatus's async cache could let a stale-base PR slip through.

  Eligible: `gh pr merge N --squash` (no `--delete-branch`, no `--auto` — see `code-review-pipeline.sh:314-322` for the worktree/detached-HEAD rationale; `--auto` would queue the merge for later and silence loud failures). On success: post Linear comment, move issue to Done.

  Not eligible: comment on the PR with the precise blocker, but **only if blockers changed** since the bot's last `Bureau merge gate` comment (sorted-line diff). This makes the script safe to run every poll interval without comment spam.

  `--dry-run` prints gate verdicts and the action without mutating anything — use to audit before trusting it.

- **`rebase-pipeline.sh`** is OFF by default because it force-pushes (mutates shared remote state). Picks from the same `BUREAU_STATE_MERGE` pool and only fires when:
  1. `mergeStateStatus == DIRTY` (real merge conflict; BEHIND/UNSTABLE explicitly NOT handled — squash-merge tolerates BEHIND, and adding BEHIND would expand the force-push surface unnecessarily)
  2. Every commit ahead of `origin/main` carries a `Co-authored-by: ...Claude...` trailer. Any human commit in the divergence → skip with explanatory comment. Humans rebase their own branches.

  Rebase succeeds → `git push --force-with-lease` → move issue back to Build Review (re-trigger code-review against the new base by state move, NOT by gaming `pick_issue` with marker comments). Conflict on rebase → `git rebase --abort` + label `needs-human` + comment.

  `--dry-run` prints the gates and the intended action without rebasing or pushing.

**Routing change in `code-review-pipeline.sh`** (the only edit to the existing review pipeline): the APPROVE branch is split. When `agents.merge: true` AND `BUREAU_STATE_MERGE` is set, code-review moves the issue to Merge state and posts "awaiting merge gate" — the new merge agent takes over. Otherwise (default), code-review keeps the original behavior: squash-merge inline, move to Done. Backward-compatible — repos that don't opt into merge see no behavior change.

**Important:** opting in to merge does NOT auto-approve anything. Code review still has to pass the 3-cycle loop-breaker first. The 3-cycle escalation to needs-human is a deliberate protection, not something to optimize around. Two human eyes on a first APPROVE remains cheap and is not a goal of this pipeline.

### Bounded retry loop in implement-pipeline.sh

`implement-pipeline.sh` runs Claude inside a `for (( i=1; i<=MAX_ITER; i++ ))` loop instead of a single call. Each iteration: invoke `$CLAUDE`, parse the strict JSON status block via `parse_claude_json`, push whatever was committed, decide whether to continue. The pipeline previously emitted that JSON contract but never read it back — every exit-0 run shipped to Build Review even on `status: PARTIAL`, leaking half-done work into review.

Knobs (env-tunable, all have safe defaults):

```bash
BUREAU_IMPL_MAX_ITER=3          # max Claude passes per tick
BUREAU_IMPL_ITER_TIMEOUT=1800   # per-iter wall-time cap (seconds)
BUREAU_IMPL_TOTAL_TIMEOUT=5400  # cumulative wall-time cap (seconds)
```

Defaults give ≤90 min worst case per tick before the issue is parked. Per-iter timeout uses `timeout` (Linux) or `gtimeout` (macOS via `brew install coreutils`); if neither is on PATH the cap degrades to cumulative-only with a WARN.

Loop invariants:

- **Push every iteration.** queue-loop's `reset_worktree` hard-resets to `origin/$BRANCH` between picks — unpushed commits would be wiped.
- **Single-strike stuck detector.** `tasks_done == 0 AND fixed_review_items == [] AND COMMITS_THIS_ITER == 0` ⇒ park the issue. Commit count is the load-bearing signal — it catches the "spent the iter debugging without marking [X]" case the tasks.md hash alone would miss.
- **Re-fetch review feedback per iteration.** Humans may add `Code Review … Changes Requested` comments mid-run.
- **No `trap ... EXIT`.** A hard crash bails via `set -e` and queue-loop sees the non-zero exit. Adding an EXIT trap would route the issue away from Build on crash — the opposite of what's wanted.

Terminal status routing:

| Status | PR | needs-human label | State move | escalations.log |
|---|---|---|---|---|
| `COMPLETE` | flipped to ready | no | → QA or Build Review | no |
| `NEEDS_HUMAN` / `STUCK` / `CAP_TIME` / `PARTIAL` | draft (visible to reviewers) | yes | none (stays in Build) | yes |

PRs open as `--draft` during intermediate iterations so QA and code-review don't trigger on half-done work; flipped to ready via `gh pr ready` only on COMPLETE.

### Escalation log: `log_escalation` helper

Every `needs-human` escalation appends one tab-separated line to `logs/escalations.log` AND fires a JSONL `emit_event "event=escalation"` to `logs/events.jsonl`. Two sinks: the TSV file is regex-matchable for operator monitors (`tail -F | grep`), the JSONL firehose stays queryable by `/bureau-learnings`.

Line format (verbatim, tab-separated):

```
2026-05-13T19:18:23Z<TAB>ESCALATED<TAB>EXP-402<TAB>code-review<TAB>cycle=3<TAB>reason="REQUEST_CHANGES exceeded max_review_cycles"<TAB>pr=56<TAB>branch=049-parliament-debate
```

Required regex (used by the test suite and external monitors):

```
^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+ESCALATED\s+([A-Z]+-\d+)\s+(\S+)\s+cycle=(\d+)\s+reason="([^"]+)"\s+pr=(\d+)\s+branch=(\S+)$
```

Hooked at every site where a pipeline labels `needs-human`:

| Site | Pipeline | Triggering condition |
|---|---|---|
| Cycle-limit + BLOCK fallthrough | code-review | `REVIEW_CYCLE_COUNT >= MAX_REVIEW_CYCLES` |
| gh-merge-fail-after-approve | code-review | `gh pr merge` non-zero or PR state != MERGED |
| BLOCK verdict | code-review | reviewer returned BLOCK (not cycle-limit) |
| NEEDS_HUMAN verdict | qa | qa flagged out-of-scope failure |
| Retry-loop terminal | implement | NEEDS_HUMAN / STUCK / CAP_TIME / PARTIAL |

Logs **only** on `add_issue_label` success — the call site uses `if add_issue_label … then log_escalation … fi`, so a Linear API hiccup doesn't produce a phantom escalation. Embedded double quotes in the reason text are scrubbed to single quotes to keep the line regex-matchable.

`bureau-config.sh` ships via `--resync-scripts`; existing target repos pick up `log_escalation` on the next resync without special wiring. Add `logs/escalations.log` to the same `.gitignore` template entry that already excludes `logs/queue-*.log`.

### Shared prompt grounding: `build_spec_context`

Every pipeline that invokes Claude for non-trivial work now calls `build_spec_context "$SPEC_DIR"` and injects the result into the prompt. The helper enumerates `SPEC.md`, `CLAUDE.md`, and the per-ticket artifacts (`spec.md`, `plan.md`, `research.md`, `tasks.md`, `design.md`) that exist, and appends the "pinned decisions win; cite the pin when declining" disciplinary paragraph. Originally only `code-review-pipeline.sh` had this grounding; spreading it fixed re-surfacing of deferred findings in spec-review and implement, and reduced REQUEST_CHANGES cycles where an implementer re-introduced a pattern code-review had already declined.

---

## Update Mode

When `$ARGUMENTS` contains `--update`:

1. **Show current effective config first.** Run `bash scripts/bureau-status.sh --config` and display the output. This surfaces both `.bureau.json` values and the env-only knobs (`BUREAU_IMPL_*`, `BUREAU_SUPERVISOR_*`, `LINEAR_API_KEY` set/unset, etc.) the user might want to change.

2. Ask: "What would you like to change?" Present these option groups:
   - **Linear** — teams, labels, states (`.bureau.json` → `.linear.*`)
   - **Agents** — toggle on/off (`.bureau.json` → `.agents.<stage>`)
   - **Tuning** — poll interval, max review cycles, concurrency cap, sampling threshold, merge strategy (`.bureau.json` → `.agents.*`)
   - **Models** — default + per-stage (`.bureau.json` → `.agents.[<stage>.]model`)
   - **Repo** — branch prefix, commit prefix, specs dir (`.bureau.json` → `.repo.*`)
   - **Retry loop** — `BUREAU_IMPL_MAX_ITER`, `BUREAU_IMPL_ITER_TIMEOUT`, `BUREAU_IMPL_TOTAL_TIMEOUT` (`.env`, env-only)
   - **Supervisor** — `supervisor.max_crashes`, `supervisor.stability_window` (`.bureau.json` OR `.env` — env overrides; ask which surface)
   - **Runtime** — `BUREAU_DRY_RUN`, `BUREAU_SESSION_NAME` (`.env`, env-only)

3. **For the selected section, edit the right surface:**
   - JSON-backed values → modify `.bureau.json` in place. Preserve unrelated keys, preserve key ordering where possible.
   - Env-only values → update `.env` inside the bureau-managed block (see below). **Never** delete or reorder unrelated entries (`LINEAR_API_KEY`, `TELEGRAM_BOT_TOKEN`, user-set vars).

4. **The bureau-managed block in `.env`:** Bureau-init manages env-only knobs inside delimited markers so the user can hand-edit secrets and other vars above the block freely.

   ```
   # User-set entries (LINEAR_API_KEY, TELEGRAM_*, etc.) live above this line.

   # ── bureau-init managed (do not edit between markers; run /bureau-init --update instead) ──
   BUREAU_IMPL_MAX_ITER=5
   BUREAU_IMPL_ITER_TIMEOUT=2400
   BUREAU_DRY_RUN=0
   # ── end bureau-init managed ──
   ```

   When updating: locate the markers (or append the block at end of file if absent), rewrite ONLY the lines between them. Drop a line by setting it to its default value (the script reads `${VAR:-default}` style, so the line just exists as documentation in that case — or remove the line entirely; both are equivalent at runtime).

5. **Regenerate affected scripts** only if `.bureau.json` changed. Env-only edits don't trigger script regeneration — they're read at pipeline runtime.

6. **Re-run `bash scripts/bureau-status.sh --config`** and show the diff between before/after. Confirm with the user before closing.

**Source legend in the status output:**
- `json` — value comes from `.bureau.json`
- `env *` — env var is set and takes precedence over any `.bureau.json` value
- `def` — value is the in-code default (env unset, no `.bureau.json` key)

### Per-agent model selection (callable routine)

Invoked when the user picks the "Models" option group in `--update`, or
opts in at the tail of `--resync-scripts`. The goal: let the operator
choose a model per stage without hand-editing `.bureau.json`.

Background: each `*-pipeline.sh` builds its `claude -p` invocation via
`claude_cmd_for_stage <stage>` in `bureau-config.sh:141-154`. That function
resolves the model in this order (first non-empty wins):

1. `BUREAU_MODEL_<STAGE>` env var (uppercase stage name)
2. `.agents.<stage>.model` in `.bureau.json`
3. `.agents.model` in `.bureau.json` (workspace-wide default)
4. *(no `--model` flag — claude CLI default)*

Stages with their own slot: `spec`, `spec_review`, `ux`, `copy`,
`implement`, `qa`, `code_review`, `merge`. (Triage and rebase never call
Claude — they're glue.)

Recommended model defaults at the time of writing (Mar 2026):

| Stage         | Cost-aware default | Quality-aware default |
|---------------|--------------------|-----------------------|
| spec          | sonnet             | opus                  |
| spec_review   | sonnet             | opus                  |
| ux            | sonnet             | opus                  |
| copy          | haiku              | sonnet                |
| implement     | opus               | opus                  |
| qa            | haiku              | sonnet                |
| code_review   | sonnet             | opus                  |
| merge         | haiku              | sonnet                |

These reflect: implement is the heavy-lift stage where quality earns its
keep; review/spec stages benefit from larger context but can use sonnet for
routine work; digest/copy/qa stages are mechanical and don't need opus.
Update the table when new model tiers land.

**Prompt flow:**

1. **Show current per-stage models.** Run `bash scripts/bureau-status.sh --config`
   and surface the MODELS section. If every stage is `(inherits default)` and
   the default is `(claude CLI default)`, tell the user "all stages currently
   use the Claude CLI default — set an explicit model per stage to control
   cost/quality" before prompting.

2. **Ask: "Do you want to set models per stage, or a single workspace-wide default?"**
   Two paths:
   - **Workspace default**: one prompt — "Which model for ALL stages?
     `opus` / `sonnet` / `haiku` / specific model ID / skip". Writes
     `.agents.model` in `.bureau.json`. Leaves per-stage slots empty.
   - **Per stage**: iterate the 8 stages above (or only the ones enabled
     via `agents.<stage>: true`). For each, show the recommended defaults
     and ask. The operator can answer with `opus` / `sonnet` / `haiku`,
     a specific model ID (e.g., `claude-haiku-4-5`), `skip` (leave
     unset — inherits default), or `default` (set to the workspace-wide
     `.agents.model`).

3. **Write to `.bureau.json` in place.** Preserve unrelated keys. Set
   `.agents.<stage>.model` for per-stage choices; set `.agents.model` for
   the workspace default. Removing a per-stage model = deleting the
   `.model` key under that stage (or removing the whole `.agents.<stage>`
   object if `agents.<stage>` was a bool toggle before).

4. **Confirm**: re-display the MODELS section from `bureau-status.sh --config`
   and ask the user to confirm. If they want to change, loop.

**Idempotency:** if a per-stage model already matches the operator's
choice, this is a no-op for that stage. If they pick `skip` for a stage
that already has a model set, ASK before removing — "Stage `qa` is
currently set to `claude-haiku-4-5`. Skip means inheriting the workspace
default — remove the explicit setting? [y/N]"

**Bash 3.2 safety:** when generating `.bureau.json` edits, use `jq` with
file rewrites (read, modify, atomic mv). Don't use `declare -A` for the
stage list — parallel indexed arrays only.
