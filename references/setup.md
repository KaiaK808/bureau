# Setup

Use the target repository root, including an app worktree root. Resolve `BUREAU_INIT_ROOT` from the actual loaded SKILL.md location; resolve symlinks. Quote it in shell commands. Do not assume the skill is in the target checkout or under `~/.claude`.

Before discovery, choose `--target claude|codex|both` (default to the current assistant) and interactive or background mode. For a new dual installation, choose the active Spec Kit integration explicitly. Adding a second target preserves the existing active integration unless the user requests a switch.

Run `python3 "$BUREAU_INIT_ROOT/scripts/bureau_install.py" check --repo "$PWD" --target TARGET --mode MODE`. Interactive setup needs Python 3.9+, git, jq and curl; background also checks the selected provider CLIs and tmux. GitHub actions need authenticated `gh`; Spec Kit installation needs `specify-cli` pinned to 0.7.5 (installed with `uv tool install specify-cli==0.7.5` if missing). Do not require Claude or tmux for app tasks. Do not start a background worker as part of setup. The shared background adapter supports Claude and Codex; installing an interface alone does not select a runner or qualify the adopting project's workload.

Reuse configuration and choices already given. Collect agent selection before mapping optional states. Do not replace an existing `.bureau.json`: use [update.md](update.md) for requested changes and continue with asset installation.

### Phase 1 — Linear Discovery (interactive)

Use **Linear MCP tools** for all discovery. Use the authenticated tools available in the current assistant — no API key needed.

If MCP tools are not available (no Linear tools), fall back to `curl` + `LINEAR_API_KEY` from `.env`.

**Step 1a: Discover teams**

Call the available Linear list-teams tool to get all teams. Present as a numbered list.

Explain that one Bureau configuration operates one active Linear team, then ask: **"Which team should the pipeline use?"** Reuse a team already selected by the user. Store that team as the single entry in `linear.teams`; the runtime uses `teams[0]` and does not dispatch other entries. Preserve existing multi-entry configurations during updates, but explain that only the first entry is active rather than promising multi-team operation.

**Step 1b: Discover projects**

For the selected team, call the available Linear list-projects tool and filter by team.

Present projects with their status. Ask: **"Which projects should the pipeline pull from? (comma-separated numbers, or 'all' for all started projects)"**

**Step 1c: Discover labels**

Call the available Linear list-issue-labels tool for the selected team.

Ask: **"Which label marks issues as AI-eligible? (the pipeline only picks up issues with this label)"**
Default suggestion: look for "lane-2" or similar. If none exists, offer to create one using the available Linear create-issue-label tool.

Also ask about additional labels:
- **needs-human** label (for escalation) — find or create
- **needs-ux** label (for UI routing) — find or create
- **ai-implementable** label (alternative eligibility) — find or create
- **needs-copy** label (optional — for copywriter routing; only prompt if user enabled the `copy` agent in Phase 2)
- **rebase-needed** label (optional — set by merge-pipeline.sh when a PR is wedged on `mergeStateStatus=DIRTY` *and* the divergence is bureau-only, so the kanban surfaces "wedged on rebase" vs "wedged on review/CI". Pure visibility — the rebase agent picks the PR up regardless of the label. Only prompt if the user enabled the `merge` or `rebase` agent in Phase 2.)

**Step 1d: Discover workflow states**

Call the available Linear list-issue-statuses tool for the selected team.

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

The pipeline scripts use direct Linear API calls for fast, cheap operations (state validation, issue moves, cycle counting) instead of spawning model sessions. This needs an API key.

Check if `LINEAR_API_KEY` is available:

```bash
([ -f .env ] && grep -q LINEAR_API_KEY .env) && echo "KEY_FOUND" || echo "KEY_MISSING"
```

If missing, show this guide:

> **Linear API key setup**
>
> Background scripts use direct API calls for all Linear operations. This avoids spending tokens on trivial actions.
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

Do NOT block setup on this — the `.bureau.json` and scripts can be generated without it. The key is needed before shared app stages or background runs query/change Linear.

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
- code-review is a JUDGMENTAL reviewer — reviews the pinned PR diff and posts findings. Its disposable worker may merge the PR's target locally for a build check; it does not publish that validation merge. Catches logic bugs, security, spec drift.

Copy is opt-in and only useful for repos with significant user-facing UI. It reads a voice guide (markdown file you point at via `copy_voice_file` in .bureau.json) and edits button labels, error messages, empty states, etc.

Merge closes the loop. Without it, code-review APPROVE invokes the same gated merge pipeline inline. BUREAU_NO_MERGE=1 or --no-merge stops before either route. With merge enabled, code-review APPROVE moves the issue to a new "Merge" Linear state and merge-pipeline.sh picks up from there with extra gates: PR must be OPEN, mergeStateStatus must be CLEAN, latest verdict must still be APPROVE / AUTO_APPROVE, no unresolved review threads, no needs-human/blocked/wip label. Enabling merge does NOT auto-approve — review must still pass first.

Rebase is OFF by default because it force-pushes (mutates shared remote state). It only fires when mergeStateStatus is DIRTY (real conflict, not BEHIND) AND every commit ahead of main passes the conservative Bureau ownership check (including Bureau-Generated trailers and legacy Claude commits). If a human commit is in the divergence, it skips and posts a comment.
```

Also ask:
- **Poll interval**: "How often should agents check for work? (minutes, default: 30)"
- **Workbench panes**: "How many interactive CLI sessions in the workbench? (default: 2 for background mode; 0 for app mode)"
- **Branch prefix**: "Branch prefix for feature branches? (default: feat)" — suggest based on team key

For background operation, select `agents.runner` and any per-stage runner/model overrides separately from the installed host interfaces. Preserve existing Claude model ownership during updates; use provider-specific Codex settings where v1 compatibility applies. Read [background operations](operations.md) only when the user needs a driver, single-flight mode, usage/cost controls or upstream-port configuration. Do not insert an unrelated upstream repository or project-specific build command into a new configuration.

---

### Phase 3 — Write .bureau.json

Generate the config file from all collected information:

```json
{
  "version": 2,
  "linear": {
    "teams": [
      {
        "key": "TEAM",
        "id": "team-uuid",
        "name": "Example Team",
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
    "runner": "claude",
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
    "max_concurrent_issues": 0,
    "code_review_sampling_threshold": 500,
    "merge_min_required_checks": 1,
    "use_goal_loop": false,
    "headroom_wrap": false,
    "caveman_level": "off"
  },
  "session": {
    "cost_tracking": false,
    "usage_threshold_pct": 80,
    "pause_on_stale_data": false
  },
  "repo": {
    "branch_prefix": "feat",
    "commit_prefix": "CLI",
    "specs_dir": "specs",
    "copy_voice_file": "docs/copy-voice.md",
    "path_prefix_strip": ""
  }
}
```

Use the legacy Claude runner default unless the user selected a different background provider; app tasks use their selected model. Capture the actual `repo.test_command` before enabling Codex background implementation. Write the file. Then add `.bureau.json` to `.gitignore` if not already there (it contains UUIDs that are workspace-specific).

**Token-efficiency flags** (`use_goal_loop`, `headroom_wrap`, `caveman_level`) default to OFF in the example above. Don't prompt the user during initial /bureau-init — they're per-repo opt-in via `.bureau.json` edits after setup. Full documentation: `docs/token-efficiency.md` and the `.agents.use_goal_loop` / `.agents.headroom_wrap` / `.agents.caveman_level` entries in `docs/configuration.md`. These options currently apply to the Claude background runtime; enable them only when requested and follow docs/token-efficiency.md.

---


## Install Spec Kit and Bureau assets

Preview then apply the two operations below. Substitute the chosen target; include `--active-integration codex` or `claude` when selecting or switching the active integration. Read conflicts before applying; the resync reference explains adoption of existing files.

```sh
python3 "$BUREAU_INIT_ROOT/scripts/bureau_install.py" speckit --repo "$PWD" --target TARGET
python3 "$BUREAU_INIT_ROOT/scripts/bureau_install.py" speckit --repo "$PWD" --target TARGET --apply
python3 "$BUREAU_INIT_ROOT/scripts/bureau_install.py" assets --repo "$PWD" --target TARGET --scope interfaces --scope scripts --scope workflows
python3 "$BUREAU_INIT_ROOT/scripts/bureau_install.py" assets --repo "$PWD" --target TARGET --scope interfaces --scope scripts --scope workflows --apply
```

The helper discovers existing Spec Kit hosts from active metadata and native skills, including legacy installations without a Bureau manifest. Adding a host preserves the active integration unless `--active-integration` explicitly selects another. It installs Spec Kit with the active integration last, preserves an existing constitution, and restores active metadata after a failed init. The helper compares upstream Spec Kit manifests and restores customized skills/templates and existing project instructions, because `specify --force` overwrites host skills. On failure, inspect partial assets before retrying. Verify `.specify/integration.json` and the installed skill directories.

Bureau commands are copied to `.claude/commands` and/or rendered into `.agents/skills`; managed instruction sections go into CLAUDE.md and/or AGENTS.md. Scripts are copied verbatim from templates, never reconstructed from prose. Keep user text outside the marked sections. The `.bureau-install.json` manifest records the installed hashes and targets and is gitignored.

Offer CI scaffolding when the target lacks CI: `assets --scope ci` previews `.github/workflows/ci.yml`; add `--apply` to install. It uses `runs-on: ubuntu-latest`. Preserve an existing workflow unless replacing it is requested. Self-hosting a public fork PR workflow exposes the runner to fork-PR-RCE; do not change the hosted default during ordinary installation.

Add `.bureau.json`, `.env`, `logs/`, and `.worktrees/` to the target's gitignore without removing existing entries. Create `.env.example` only if absent, with empty LINEAR_API_KEY and optional Telegram entries. Never copy or print real credentials. Shared app stages and background operation need the API key; authenticated Linear tools can serve discovery.

Validate JSON, run `bash -n scripts/*.sh` one file at a time, inspect installed targets and preview again to confirm no drift. Report installed interfaces, active Spec Kit integration, skipped conflicts and remaining prerequisites. Suggest `$speckit-constitution` in Codex or `/speckit-constitution` in Claude if the constitution is still a template. Do not overwrite a completed constitution.

For Codex targets, the `bureau` operator skill supports current-task execution. Its `references/app-setup.md` lists the setup and action commands to add through the app's supported local-environment settings. Preserve existing settings; show the commands if UI access is unavailable.
