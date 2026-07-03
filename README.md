# Bureau

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-skill-8B5CF6)](https://claude.ai/code)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

> A self-hosted multi-agent SDLC pipeline for [Claude Code](https://claude.ai/code). Bootstrap it into any git repo with a single slash command — `/bureau-init` — and the next time you pick up a Linear ticket, agents will write the spec, implement it, review it, and open the PR while you do something else.

![Bureau — a dim ceremonial archive chamber: a floor-to-ceiling wall of small dark wooden drawers, one glowing in confident hot pink, a single robed operator in the foreground reading a file card](docs/assets/hero.png)

Bureau turns the spec-review-implement-test-merge ceremony into autonomous work that runs locally, in tmux, against your existing tools. **No service to host, no backend to babysit** — state lives in Linear and your git repo. Stop watching a ticket through QA at 11pm; let the bureau handle it.

## Why Bureau

- **Spec-driven by default.** Every ticket goes through [speckit](https://github.com/github/spec-kit) — `/speckit-specify` → `/plan` → `/tasks` — before any code is written. The agents work from artifacts you can read, not from prompt-engineering memory.
- **State machine lives where you already work.** Linear is the source of truth. The agents move tickets `Triage → Spec → Spec Review → Build → QA → Build Review → Merge → Done`. Your existing Linear views, filters, and notifications keep working unchanged.
- **Token-efficient by design.** Three opt-in compression layers (`/goal`, [caveman](https://github.com/JuliusBrussee/caveman), [Headroom](https://github.com/headroomlabs-ai/headroom)) compose for ~80% fewer tokens per pipeline tick. See [docs/token-efficiency.md](docs/token-efficiency.md).
- **Loud-failure contract.** Every pipeline stage exits with a classified code; failures route to `needs-human` with a Telegram alert (optional). The merge gate enforces green CI *independently* of GitHub's `mergeStateStatus` — no silent regressions.
- **Open-source, MIT-licensed.** Built on Claude Code's skill system; composes with [speckit](https://github.com/github/spec-kit), [caveman](https://github.com/JuliusBrussee/caveman), [Headroom](https://github.com/headroomlabs-ai/headroom), and whatever AI coding tools you already use.

---

## Quickstart

1. Install the skill (§[Installation](#installation))
2. In Claude Code, run `/mcp` and authenticate `linear-server`
3. Add your Linear API key to your repo's `.env` — see [Prerequisites](#prerequisites)
4. In a git repo, run `/bureau-init` and answer the prompts
5. Start the pipeline: `./scripts/start-bureau-v2.sh`
6. Attach to watch it: `tmux attach -t bureau-v2-$(basename "$PWD")`

---

## Documentation

| Doc | When to read |
|---|---|
| [Configuration reference](docs/configuration.md) | Every `.bureau.json` key + every `BUREAU_*` env var, with defaults and resolution order |
| [Exit codes & alerts](docs/exit-codes.md) | What each pipeline failure code means and how Telegram alerts behave |
| [Recipes](docs/recipes.md) | Common config patterns: single-flight, mixed models, dry-run, multi-repo |
| [Token efficiency](docs/token-efficiency.md) | `/goal`, caveman, headroom — three opt-in layers that compose for ~80% per-tick token reduction |
| [Troubleshooting](docs/troubleshooting.md) | When things break — symptom → fix |
| [Landscape](docs/landscape.md) | Competitive prior art and where Bureau fits |

A styled HTML rendering of the same docs lives at [`docs/site/index.html`](docs/site/index.html) — open in a browser, or serve locally with `python3 -m http.server -d docs/site 8000`. Same content as the markdown files; designed for human reading rather than Claude consumption.

---

## Prerequisites

Install these once on your machine:

| Tool | Why |
|---|---|
| [Claude Code](https://claude.ai/code) | Host for the skill |
| `git` | Obviously |
| `jq` | JSON parsing in the pipeline scripts |
| `tmux` | Runs the agents in persistent panes |
| `curl` | Headless Linear API calls |
| [Linear account](https://linear.app) | Source of truth for issues |
| [GitHub CLI (`gh`)](https://cli.github.com) *(recommended)* | PR creation by the `implement` agent |

macOS one-liner:

```sh
brew install jq tmux gh
```

**Claude Code — Linear MCP.** Run `/mcp` inside a Claude Code session and authenticate `linear-server`. The skill uses MCP for interactive discovery (teams, projects, labels, states) during setup. No API key needed at this stage.

**Linear API key — for the headless agents.** The agents running in tmux use direct REST calls for fast, cheap operations. They need a key per repo in `.env`:

```sh
# Get one at https://linear.app/settings/account/api — name it "bureau-pipeline"
echo 'LINEAR_API_KEY=lin_api_xxxxxxxxxxxx' >> .env
```

The key is the same across all your repos — generate once, paste everywhere.

---

## Installation

### Option A — git clone (recommended)

```sh
mkdir -p ~/.claude/skills
git clone https://github.com/KaiaK808/bureau.git ~/.claude/skills/bureau-init
```

To pick up future updates, just `git pull` in `~/.claude/skills/bureau-init/`.

### Option B — unzip distribution

If you were handed a `bureau-init.zip`:

```sh
unzip bureau-init.zip
mkdir -p ~/.claude/skills
mv bureau-init ~/.claude/skills/
```

### Then, either way

Restart Claude Code (or start a new session). `/bureau-init` should now autocomplete in the slash-command menu.

---

## The `/bureau-init` command

| Form | What it does |
|---|---|
| `/bureau-init` | First-time setup in a new repo. 8-phase interactive walk-through (Phase 0–7): prereqs → Linear discovery → agent selection → writes `.bureau.json` → speckit init → installs slash commands + pipeline scripts → CLAUDE.md + `.env` + CI scaffold → validation |
| `/bureau-init --update` | Change teams / labels / states / agents / poll interval / branch prefix. Re-runs Linear discovery only for the sections you pick. Leaves everything else alone |
| `/bureau-init --resync-scripts` | Pull in the latest pipeline scripts from the skill template. Per-file confirmation; local customizations are preserved unless you confirm an overwrite. Does NOT touch `.bureau.json`, `.specify/`, or `.claude/skills/speckit-*` |
| `/bureau-init --resync-speckit` | Re-run `specify init` for the bureau-init-pinned spec-kit version (currently v0.7.5). Backs up + restores `.specify/memory/constitution.md`. Refreshes `.specify/templates/`, `.specify/scripts/`, and `.claude/skills/speckit-*/SKILL.md`. Does NOT touch pipeline scripts or `.bureau.json` |
| `/bureau-init --help` | Open the bundled HTML documentation (`~/.claude/skills/bureau-init/docs/site/index.html`) in your default browser. Works regardless of whether a repo is initialized. Falls back to printing the `file://` URL on headless / SSH sessions |

### First-time walk-through (summary)

1. **Phase 0** — checks prereqs and that you're in a git repo
2. **Phase 1** — Linear: pick teams, projects, the eligibility label (e.g. `lane-2`), and map workflow states to pipeline stages (Triage → Spec → Spec Review → Design → Copy → Build → QA → Build Review → Merge → Done; Design, Copy, QA, and Merge are opt-in and only prompted for when the corresponding agent is enabled)
3. **Phase 2** — pick which agents to enable (`spec`, `spec-review`, `ux`, `copy`, `implement`, `qa`, `code-review`, `merge`, `rebase`), poll interval, workbench panes. `merge` and `rebase` are off by default; `rebase` force-pushes to remote and is opt-in for that reason
4. **Phase 3** — writes `.bureau.json` (gitignored; contains workspace UUIDs)
5. **Phase 4** — initializes speckit by delegating to `specify init --here --integration claude` (pinned to v0.7.5). Installs `.specify/` + native skills under `.claude/skills/speckit-*/SKILL.md`
6. **Phase 5** — installs Bureau's Linear slash commands at `.claude/commands/` and pipeline scripts at `scripts/`
7. **Phase 6** — writes / merges `CLAUDE.md`, creates `.env.example`, updates `.gitignore`, and offers to scaffold `.github/workflows/ci.yml`
8. **Phase 7** — validation pass: sanity-checks the install and prints a summary report

---

## After setup — what lands in your repo

```
.bureau.json              # your config — UUIDs, agent choices (gitignored)
.specify/                 # speckit scaffolding (installed by `specify init`)
  memory/constitution.md  # project principles (fill this in early)
  templates/              # spec, plan, tasks, checklist, constitution templates
  scripts/bash/           # speckit helpers (check-prerequisites, create-new-feature, …)
  integrations/*.json     # per-file SHA256 manifests (used to detect drift)
  workflows/              # speckit's interactive workflow yaml — bureau-init bypasses this
.claude/skills/           # native speckit skills: /speckit-specify, /speckit-plan, …
.claude/commands/         # Bureau's Linear commands: /linear-to-spec, /check-linear-queue, /bureau-learnings, …
.github/workflows/ci.yml  # opt-in: scaffolded CI. Defaults to ubuntu-latest; self-hosted is an opt-in for private, non-production runners
LESSONS.md                # optional, committed: human-curated learnings drafted by /bureau-learnings
logs/events.jsonl         # one JSONL event per pipeline stage run (gitignored, mined by /bureau-learnings)
logs/escalations.log      # one TSV line per `needs-human` escalation (gitignored, designed for `tail -F | grep`)
scripts/
  # ─── Drivers (what you invoke) ───────────────────────────────────────
  start-bureau-v2.sh         # continuous mode — launches the tmux pipeline (one window per agent, cron-friendly)
  start-agents.sh            # lower-level: start a single agent's queue-loop in the current pane
  shepherd.sh                # single-ticket end-to-end — runs one ticket through every stage sequentially
  orchestrate.sh             # batch mode — plans + executes N tickets in parallel-safe worktree lanes
  upstream-port.sh           # fast-path cherry-pick from a configured upstream, skipping the shepherd ceremony
  bureau-status.sh           # read pipeline state without attaching to tmux

  # ─── Per-stage workers (queue-loop calls these) ──────────────────────
  spec-pipeline.sh           # Triage → Spec
  spec-review-pipeline.sh    # Spec → Spec Review
  ux-pipeline.sh             # opt-in: needs-ux → Design → Build
  copy-pipeline.sh           # opt-in: needs-copy → Build
  implement-pipeline.sh      # Build → QA (or Build Review if QA off)
  qa-pipeline.sh             # opt-in: runs tests, writes missing ones, → Build Review or bounces to Build
  code-review-pipeline.sh    # Build Review → Merge or Changes-Requested-back-to-Build
  merge-pipeline.sh          # opt-in: gated merge — pr_ci_is_green + pr_base_is_current + no blocking labels
  rebase-pipeline.sh         # opt-in: force-pushes to remote when a branch falls behind main

  # ─── Loop mechanics + helpers ────────────────────────────────────────
  queue-loop.sh              # per-agent poller — one process per stage, cron tick fires the matching pipeline
  queue-loop-supervised.sh   # queue-loop wrapper with auto-restart + backoff on crash
  bureau-config.sh           # reads .bureau.json (sourced by every pipeline script)
  codex-stage-runner.sh      # backend for stages routed to Codex (opt-in via agents.<stage>.runner=codex)
  crosscheck-specs.sh        # dry-run helper: reports spec-vs-in-flight PR file collisions before you shepherd
  setup-merge-drivers.sh     # installs git merge drivers (union for CHANGELOG, ours for lockfiles)
  grab-issue.sh              # Linear issue locking helper
  complete-issue.sh          # Linear state transition helper
specs/                    # specs land here (one folder per feature)
```

---

## Running the pipeline

Each repo gets its own **tmux session** named `bureau-v2-<repo-basename>` by default, so multiple repos don't collide. A repo at `~/projects/sofa` will use session `bureau-v2-sofa`.

### Start, attach, detach, stop

```sh
# From inside the repo
./scripts/start-bureau-v2.sh

# Optional args: poll interval (minutes) + workbench pane count
./scripts/start-bureau-v2.sh 30 2

# Attach to watch (from any terminal)
tmux attach -t bureau-v2-$(basename "$PWD")

# List all active bureau sessions across all repos
tmux ls | grep bureau-v2-

# Switch between windows inside tmux: Ctrl-b <N>   (0=status, 1..=agents)
# Detach without stopping: Ctrl-b d
# Stop the pipeline
tmux kill-session -t bureau-v2-$(basename "$PWD")
```

### Override the session name

```sh
BUREAU_SESSION_NAME=nightshift ./scripts/start-bureau-v2.sh
tmux attach -t nightshift
```

### Check pipeline state without attaching

```sh
./scripts/bureau-status.sh
```

### Multiple repos in parallel

Session names are scoped by folder name, so running pipelines side-by-side just works:

```sh
cd ~/projects/sofa          && ./scripts/start-bureau-v2.sh   # → bureau-v2-sofa
cd ~/projects/brainhuggers  && ./scripts/start-bureau-v2.sh   # → bureau-v2-brainhuggers
tmux ls | grep bureau-v2-                                      # both listed
```

**Caveat:** the Linear API key is per-user across all your repos. Make sure each repo's `.bureau.json` points at a **different Linear project** — otherwise two workers will race on the same issues.

### Drive a single ticket end-to-end (shepherd)

Sometimes you want one ticket run through every stage right now — no cron ticks, no other tickets competing. That's `shepherd.sh`:

```sh
./scripts/shepherd.sh --no-tmux EXP-123
```

Runs `Triage → Spec → Spec-Review → Build → QA → Code-Review → Merge` sequentially for the named ticket, in `.worktrees/shepherd/` by default. Useful for smoke-testing your `.bureau.json` config, driving a single stuck ticket, or shipping a one-off without spinning up the whole queue. Pass `--worktree DIR` to isolate the checkout somewhere else.

### Ship a batch (orchestrate + conflict-aware-schedule)

For multiple tickets ready to go, `orchestrate.sh` runs them in parallel-safe worktree lanes. A "brain" workflow (`templates/workflows/conflict-aware-schedule.js`) predicts each ticket's file footprint, builds the collision graph, and emits:

```json
{ "serialChains": [["EXP-12","EXP-9"]], "parallelSafe": ["EXP-7","EXP-8"] }
```

File-colliders get serialized; independents run in parallel. Then:

```sh
./scripts/orchestrate.sh --execute --schedule schedule.json --max-concurrent 3
```

Each lane runs in its own git worktree (`.worktrees/shepherd-lane-N`) so builds never collide. Full walk-through of the executor pattern (SELECT → PLAN → EXECUTE → BABYSIT) lives in the [operator cheat-sheet](docs/OPERATOR-CHEATSHEET.md).

### Port from upstream (upstream-port)

If you're a fork of another repo, `upstream-port.sh` skips the shepherd ceremony for cherry-pick-shaped changes:

```sh
./scripts/upstream-port.sh --sha 53953a8               # port a single commit
./scripts/upstream-port.sh --pr  3024                  # or a merged upstream PR
./scripts/upstream-port.sh --sha 53953a8 --with-llm    # let Claude resolve conflicts
```

Configure the upstream in `.bureau.json` (`repo.upstream`, `repo.upstream_port.build_cmd`, `repo.upstream_port.test_cmd`). `--with-llm` invokes Claude *once* on a `git apply --3way` conflict to translate upstream intent against the local code; PR title carries a `(LLM-assisted)` marker. Off by default — you have to ask.

### Single-flight mode (drain before refilling)

Active repos with many concurrent feature tickets accumulate branch divergence faster than the pipeline can drain them. Symptom: every cron tick re-runs `merge_origin_main_or_abort` against an ever-advancing `origin/main`, conflicts pile up, the loop spins. Two knobs:

```jsonc
{
  "agents": {
    "max_concurrent_issues": 1   // 0 = unlimited (default). 1 = drain one issue end-to-end before Spec picks another.
  }
}
```

Combined with `queue-loop.sh`'s stage-priority reverse-sort (Merge/Rebase first, Spec last), the pipeline drains before refilling — attention goes to the tickets closest to Done. Full write-up in [recipes.md](docs/recipes.md).

### Dry-run mode

Every pipeline script honors `BUREAU_DRY_RUN=1` — instead of pushing branches, calling `gh pr create`, moving Linear states, or posting comments, it prints what it *would* have done. Safe first-run for a new `.bureau.json`:

```sh
BUREAU_DRY_RUN=1 ./scripts/shepherd.sh --no-tmux EXP-123
```

You'll see the full stage sequence, the prompts, and the "would move to state X" traces without any external mutation. Recommended for smoke-testing before you point the pipeline at a real Linear board.

### Template drift warnings

On every launch, `start-bureau-v2.sh` compares your repo's `scripts/*.sh` against the skill template. If anything differs or a new template script has appeared since your last init, you'll see:

```
⚠  bureau-init template drift: N differ, M new
   → resync with:  claude /bureau-init --resync-scripts
```

It never mutates on its own. Run `/bureau-init --resync-scripts` at your convenience — per-file confirmation on overwrites, local tweaks preserved unless you say yes.

### Upgrading an existing pipeline

New versions of the skill can add stages (e.g. QA, Copy) and tighten prompts. The target repo's scripts are refreshed via `--resync-scripts`; `.bureau.json` stays yours. Safe sequence:

1. **Pause the loop.** Let any in-flight tick finish first — `tail -F logs/queue-*.log` until idle, then `tmux kill-session -t bureau-v2-$(basename "$PWD")`. If you run it from cron, remove the cron entry for now. Don't interrupt a tick that's mid-`$CLAUDE` call; the issue can end up stranded between states.

2. **Refresh the skill source.** `cd ~/.claude/skills/bureau-init && git pull`.

3. **Resync scripts in the target repo.** `claude /bureau-init --resync-scripts`. Per-file confirmation, ~40-line diff preview, local tweaks preserved unless you confirm.

3b. **Resync speckit (only when bureau-init bumps `BUREAU_SPECKIT_VERSION`).** `claude /bureau-init --resync-speckit`. Backs up + restores your `constitution.md`. Then `rm .claude/commands/speckit.*.md` to clear stale legacy commands from any pre-v0.7.5 install (the new install lives at `.claude/skills/speckit-*/`).

4. **Don't turn new agents on yet.** New pipelines (QA, Copy, …) are opt-in via `.bureau.json`. Until you add their state UUIDs the scripts are inert — they exit 2 (queue-empty) and `queue-loop.sh` skips them. So step 3 alone is a no-op behavior change; you get tighter prompts and more defensive output parsing without any new wiring.

5. **Restart the loop.** `./scripts/start-bureau-v2.sh`. Confirm the drift banner is gone.

6. **Enabling a new agent (later, when you want it).** Order matters:
   a. **Create the state in Linear first.** If `agents.qa: true` is set but `states.qa` points at a non-existent UUID, `move_issue` fails and the source stage's EXIT trap routes the issue back — annoying but not destructive.
   b. **Edit `.bureau.json`.** Add the state UUID (`linear.teams[0].states.qa` or `.copy`), flip the agent flag (`agents.qa: true` / `agents.copy: true`). For Copy also add `linear.labels.needs_copy.name` and optionally `repo.copy_voice_file`.
   c. **Restart the loop.**

**In-flight issues are safe across the upgrade.** An issue already in Build Review keeps going to Done; an issue already in Build will route to its *new* next state (QA if you enabled it, else Build Review as before). The only immediate behavior change on step 3 is code-review: it now parses a fenced JSON block from the merger output. The legacy regex-on-prose parse is kept as a defense-in-depth fallback, so the first post-upgrade review — which may not yet emit a JSON block — still produces a verdict.

---

## How the pipeline works

| Linear state / label | Which agent acts | Result |
|---|---|---|
| Labelled `lane-2` (or your chosen eligibility label), state `Triage` | `spec` | Writes `specs/NNN-slug/spec.md` + `plan.md` + `tasks.md`, moves issue to `Spec Review` |
| State `Spec Review` | `spec-review` | Validates spec against the codebase; moves to `Build` (or `Design` if UI-heavy, `needs-human` if ambiguous) |
| State `Design` (if `ux` agent enabled) | `ux` | Generates `design.md` from spec + codebase conventions; moves to `Build` (or `Copy` if `needs-copy` label present) |
| State `Copy` (opt-in; if `copy` agent enabled + `needs-copy` label) | `copy` | Polishes user-facing strings against a voice guide (`copy_voice_file`); moves to `Build` |
| State `Build` | `implement` | Works the task list, commits, opens PR, moves to `QA` (if enabled) or `Build Review` |
| State `QA` (opt-in; if `qa` agent enabled) | `qa` | Runs the test suite, writes missing tests, commits. Green → `Build Review`; red → `Build`; harness broken → `needs-human` |
| State `Build Review` | `code-review` | Reviews PR (correctness + security + performance), comments, iterates up to `max_review_cycles` |
| Label `needs-human` anywhere | — | Pipeline stops; you take over |

**QA vs code-review** (these are complementary): `qa` is a mechanical executor — runs `npm test` / `cargo test` / etc. and writes test files when coverage is missing. `code-review` is a judgmental reviewer — reads the diff and writes a PR comment, never touches the branch. QA catches missing/broken tests; code-review catches logic bugs and security issues in paths tests don't cover. Running QA first means code-review doesn't burn tokens reviewing a PR whose build is already red.

### Pre-spec research (optional, label-gated)

Spec agents work from the issue body + codebase grep — they don't open external docs, so issues that integrate with an API, SDK, or specific library version often get specs with hallucinated method signatures and config keys. The `spec` pipeline ships with an optional pre-step that runs *before* speckit-specify, gated by:

1. The Linear label **`needs-research`** is on the issue (create the label once in your Linear workspace; apply it during issue triage).
2. `.agents.research` is configured in `.bureau.json`:
   ```json
   "agents": {
     "research": { "model": "claude-haiku-4-5-20251001" }
   }
   ```
   Haiku is usually the right default — research is doc-reading, not code-writing.

When both are true, the spec pipeline invokes Claude with WebFetch/WebSearch enabled, instructs it to compile a markdown digest of the relevant APIs (current versions, endpoints, recent breaking changes, gotchas), posts that digest as a Linear comment (visible to spec-review for traceability), and injects it into the speckit-specify prompt as authoritative API reference. The `needs-research` label is then stripped.

The whole step is **best-effort**: a failed research call (non-zero exit, missing `<!-- bureau-research: -->` marker, network down) is swallowed and the pipeline proceeds to specify without research context. No new exit codes, no new alert classes. Default is **off** — repos without `.agents.research` in their config see no behavior change.

### Memory loop (logs → `LESSONS.md`)

`queue-loop.sh` appends a structured event to `logs/events.jsonl` for every pipeline stage run (`stage_start` / `stage_end` with `issue`, `class`, `duration_s`, etc.). Once the queue has some activity, run `/bureau-learnings` to draft a `LESSONS.md` at the repo root. It clusters recurring failure modes, repeated review feedback, and stage timing across the last 30 days (≥3-issue threshold per finding — empty sections are a valid output).

The draft is **never auto-committed.** Review the diff, edit freely, then `git add LESSONS.md`. The committed file is selectively read back into the spec and code-review prompts on future runs as advisory (not pinned) context. To dismiss a finding, delete its bullet — `/bureau-learnings` will re-propose it if the pattern persists. See [recipes.md](docs/recipes.md#memory-loop-logs--lessonsmd) for details.

### Branch protection (strongly recommended)

The `merge` agent enforces two correctness gates that GitHub's `mergeStateStatus` heuristic doesn't reliably catch:

- **`pr_ci_is_green`** — every check-run on the PR's current head SHA must be `completed` + green. Catches the "CI hasn't started yet so `mergeStateStatus = CLEAN`" race that GitHub doesn't surface.
- **`pr_base_is_current`** — the PR's `baseRefOid` must equal `origin/main`'s HEAD. Catches the stale-base race where mergeStateStatus's async cache still reads CLEAN seconds-to-minutes after main moved.

Both gates re-run **just-in-time** before `gh pr merge`. If anything regressed between the initial check and the merge call, the pipeline aborts cleanly and the next tick re-evaluates against fresh state.

Defaults are strict (`merge_require_green_ci: true`, `merge_require_up_to_date: true` in `.bureau.json`). Belt-and-suspenders backstop on the GitHub side — strongly recommended:

```bash
gh api -X PUT "repos/$OWNER/$REPO/branches/main/protection" \
  -F required_status_checks.strict=true \
  -F required_status_checks.contexts[]="ci" \
  -F enforce_admins=false \
  -F required_pull_request_reviews.required_approving_review_count=0 \
  -F restrictions=null
```

Adjust `contexts[]` to your repo's actual check names. `strict=true` is the "require branches to be up to date before merging" toggle — even a pipeline bug can't merge a stale PR if GitHub itself refuses.

Repos without CI (docs-only, prototypes) can opt out of the strict gates by setting `agents.merge_require_green_ci: false` and/or `agents.merge_require_up_to_date: false` in `.bureau.json`. Discouraged; the toggles exist for genuine no-CI cases.

### Serializing dependent issues

The stock picker sorts by priority + creation time. It does **not** yet honor Linear's `blockedBy` / `blocks` relations, so a chain of tickets (A → B → C) all labelled eligible can be picked in any order once their state qualifies.

Two workarounds until dependency-awareness lands:

- **Ration the label.** Only label the next-up issue with `lane-2`. As each completes, move the label to the next. ~5 seconds per step.
- **Priority-stagger.** Make the first step High and the rest Medium. Good enough for loose ordering when strict serialization isn't essential.

---

## Tips

- **Start with a tiny issue first.** Specs are only as good as the Linear description — write acceptance criteria carefully.
- **Write `.specify/memory/constitution.md` early.** It's the project principles doc; every spec gets validated against it.
- **The `ux` agent is off by default.** Enable only if you want a `design.md` generated from mockups/screenshots.
- **Poll interval defaults to 30 min.** Lower for active work, raise for background.
- **`.bureau.json` is gitignored.** Each collaborator runs `/bureau-init` themselves — the UUIDs match a Linear workspace, not a person.
- **Agents auto-restart.** If a tmux pane dies, `start-bureau-v2.sh` re-creates it on next launch. Logs go to `logs/`.

### Complementary tools

- **[obra/superpowers](https://github.com/obra/superpowers)** is a Claude Code plugin for *interactive* design + TDD sessions — brainstorming, write-plan, subagent-driven dev, etc. Bureau handles the autonomous Linear-driven queue; superpowers fits the human-driven workbench pane. Install with `/plugin install superpowers@claude-plugins-official` from any Claude Code session — no per-repo setup needed. They don't conflict; Bureau's `claude -p` workers and superpowers' interactive skills run in different contexts.

---

## Troubleshooting

**`MISSING: <tool>` at Phase 0** — install the tool, re-run `/bureau-init`.

**`This repo already has a bureau pipeline configured`** — run `/bureau-init --update` to change config, `/bureau-init --resync-scripts` to pull in template script updates, `/bureau-init --help` to open the docs in a browser, or `rm .bureau.json` to start from scratch.

**Linear MCP not available during setup** — in Claude Code, run `/mcp`, authenticate `linear-server`, restart the session.

**Agents running but not picking up issues** — check the issue has the correct eligibility label (e.g. `lane-2`) *and* is in the `Triage` state for the team configured in `.bureau.json`.

**`LINEAR_API_KEY` not set** — interactive setup will complete without it, but agents fail on first run. Add it to `.env` before `start-bureau-v2.sh`.

**`⚠ bureau-init template drift: N differ, M new`** — the skill template has been updated since this repo was initialized. Run `/bureau-init --resync-scripts` — per-file confirmation, nothing overwritten silently.

**`WARNING: Old 'bureau' session is still running`** — harmless. The script checks for a legacy session literally named `bureau` (no `-v2`). If you never ran the v1 pipeline you can ignore it, or delete the check block at the top of `start-bureau-v2.sh`.

**tmux session already exists on startup** — `start-bureau-v2.sh` kills its own session (`bureau-v2-<basename>`) before recreating it, so restarts are clean. If you see unrelated stale sessions, `tmux ls` + `tmux kill-session -t <name>`.

---

## Repo layout (for contributors)

```
SKILL.md                       # 8-phase flow (Phase 0–7) Claude follows when /bureau-init fires. Source of truth.
README.md                      # User-facing install + usage (this file).
CLAUDE.md                      # Claude Code guidance for this repo.
CONTRIBUTING.md                # How to try Bureau, file a bug, send a PR. Read before opening one.
SECURITY.md                    # Vulnerability reporting policy.
CHANGELOG.md                   # Notable changes, PR-link driven.
NOTICE                         # Third-party asset provenance.
docs/landscape.md              # Competitive landscape research (reference).
templates/
  scripts/*.sh                 # Pipeline scripts. Copied verbatim into the target repo's scripts/.
  commands/*.md                # Slash-command definitions. Copied into the target repo's .claude/commands/.
  workflows/*.js               # "Brain" workflows (e.g. conflict-aware-schedule). Copied into .claude/workflows/.
  .github/workflows/ci.yml     # Optional CI scaffold. Copied to target repo on Phase 6 confirmation.
```

Note: **spec-kit is not vendored here** — Phase 4 delegates to the upstream `specify` CLI (pinned via `BUREAU_SPECKIT_VERSION`). No `templates/speckit/` directory exists.

**Working rule:** when `SKILL.md` and a template disagree, the template wins. Update `SKILL.md` to match.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR workflow, test-suite invocation (`bash tests/run.sh`), and what's out of scope.

---

## Origin & license

Built by [Kai Ebert](https://github.com/KaiaK808) / Brainhuggers. Open-sourced under the MIT license — see [LICENSE](LICENSE). No warranty; this is a dev tool that writes code and talks to your Linear workspace, so run it on repos you're comfortable auto-committing to. Security policy in [SECURITY.md](SECURITY.md); code of conduct in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
