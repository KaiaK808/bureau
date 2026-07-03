# Bureau Operator Cheat-Sheet

For an **agent driving the pipeline** in a repo that has bureau-init installed. Quick reference; deeper detail in [`configuration.md`](configuration.md), [`recipes.md`](recipes.md), and [`exit-codes.md`](exit-codes.md).

## Mental model

Issues are the unit of work. A ticket flows **Triage → Spec → Spec-Review → Build → QA → Code-Review → Merge → Done**, one LLM agent per stage. You don't write the code — you write a good **issue body** (it becomes the build spec) and drive the machinery.

Two kinds of artifact, on purpose:
- **Deterministic bash** (`scripts/`): mechanical, cron-friendly, resumable — `shepherd.sh`, `orchestrate.sh`, the `*-pipeline.sh` stages.
- **Brains** (`templates/workflows/*.js`, run as workflows): judgment that can't be hard-coded — e.g. `conflict-aware-schedule`. Brains *plan*; bash *executes*. The handshake is a small JSON file.

## Setup (once per repo)

1. Run `/bureau-init` → scaffolds `scripts/`, `.bureau.json`, Linear + speckit wiring.
2. Edit `.bureau.json`: Linear team + state IDs, project, per-stage `model`, optional per-stage `runner`. See [`configuration.md`](configuration.md).

## Drive ONE ticket

```bash
scripts/shepherd.sh --no-tmux EXP-123
```
Runs that ticket through every stage to a terminal state. Builds in `.worktrees/shepherd` by default; pass `--worktree DIR` for a per-ticket checkout.

## Run a BATCH — the executor (the agent as conductor)

Four moves:

### 1. SELECT — *decide what to shepherd*
Pull **Triage** tickets that are well-specified (real spec-grade body), labelled build-ready (e.g. `ai-implementable`), and unblocked. Prefer independent + cheap first; respect dependencies. (Backlog/upstream-triage *brains* are repo-specific add-ons, not bundled here — for single upstream commits use `scripts/upstream-port.sh`.)

### 2. PLAN — *which to parallelize* (don't decide by hand)
Run the **`conflict-aware-schedule`** workflow (`templates/workflows/conflict-aware-schedule.js`) over the chosen tickets. It predicts each ticket's **file footprint**, builds the collision graph, and emits:
```json
{ "serialChains": [["EXP-12","EXP-9"]], "parallelSafe": ["EXP-7","EXP-8"] }
```
File-colliders are serialized; independents run in parallel. You can also hand-write this JSON.

### 3. EXECUTE — *run it concurrently, on budget*
```bash
scripts/orchestrate.sh --execute --schedule schedule.json --max-concurrent 3
```
Each lane runs in its **own git worktree** (`.worktrees/shepherd-lane-N`) → builds never collide. Levers:

| Lever | How | Buys you |
|-------|-----|----------|
| Concurrency | `--max-concurrent N` | parallel lanes (set N ≈ cores **and** quota headroom) |
| Off-quota review | `BUREAU_RUNNER_CODE_REVIEW=codex` (or `.agents.code_review.runner` in `.bureau.json`) | `code_review` runs on Codex, off the Claude session quota |
| Throttle | `.bureau.json` `usage_threshold_pct` + a wired usage signal | long unattended runs **pause near the quota limit** instead of stranding mid-build |
| Cost visibility | `.bureau.json` `"cost_tracking": true` → `scripts/bureau-status.sh --cost` | per-stage $/token burn, so you size the next batch |

### 4. BABYSIT + LAND
Watch stage transitions (tail the orchestrate log). On green gates `merge-pipeline.sh` merges → **Done** autonomously. On a `needs-human` park → check the PR's **real CI** before believing it (in-sandbox QA false-negatives happen), then merge if green or fix/re-spec.

### How it clicks together
```
backlog → conflict-aware-schedule (BRAIN: plan)
              ↓ schedule.json { serialChains, parallelSafe }
         orchestrate.sh --execute --max-concurrent N (BASH: concurrent worktree lanes)
              ↓ per lane
         shepherd.sh → spec → … → qa (Claude) → code_review (Codex) → merge
              ↓
         bureau-status --cost · throttle pauses near limit · you merge parks on green CI
```

### Decision heuristics the agent owns
- **Batch size** ← cost budget (cost tracking informs it; throttle protects it).
- **Concurrency cap** ← cores ∧ quota headroom.
- **Gate-on-human vs merge-if-green** ← is it outward-facing / taste-critical / first-of-a-kind? If yes, gate; else merge-if-green.

## Runner / cost — and the one hard rule

- `code_review` → **Codex** is safe + cheap (reads diffs only, off the Claude quota). Set `.agents.code_review.runner="codex"` or `BUREAU_RUNNER_CODE_REVIEW=codex`.
- ⚠ **NEVER route `qa` (or any stage that runs the build/test suite) to Codex.** Codex's `exec` sandbox has no network listeners, trust-store, or git-metadata writes, so real suites fail spuriously → the stage false-halts `needs-human`. Keep **qa / implement / spec on Claude.** (`claude_cmd_for_stage` emits a stderr warning if you try.)

## Hard rules / gotchas

- **The issue body IS the spec.** Vague body → vague build. Always *What / Why / Target-modules / Acceptance* (+ a red-line for behaviour features).
- **Worktree isolation → builds never conflict.** All conflicts are at **merge**, on shared files (a command registry/enum, dispatch arms, auto-gen docs). Mechanical: `git merge origin/main` per PR. Batching N features that each edit the *same* registry file = N−1 small keep-both resolutions.
- **Real CI is the truth, not the in-pipeline QA.** Always verify a parked ticket's actual CI.
- **Wrong-scope build** (spec generated from a stale body) → close the PR, fix the body, relaunch, gate the regenerated spec *before* build.
- **Branch before committing**; never push the default branch directly.
- **Exit codes are classified** (`{0 ok, 14 build, 15 test, 17 conflict, 18 gh/guard}`) — halts are loud, never silent. See [`exit-codes.md`](exit-codes.md).

## CI (the merge gate's source of truth)

`merge-pipeline.sh` enforces **green CI** independently of GitHub's `mergeStateStatus`, so the repo needs a real CI workflow. `/bureau-init` offers to scaffold one at `.github/workflows/ci.yml` (also `/bureau-init --resync-ci` to refresh it).

- Default `runs-on: ubuntu-latest` (GitHub-hosted, safe for public repos). Swap to your own `[self-hosted, ...]` labels only if the runner is private + non-production.
- The `- run: bash tests/run.sh` step is a **placeholder** — replace with your repo's real build/test command (the file carries commented Rust + bureau-init examples).
- ⚠ **Self-hosted runners on public repos execute fork-PR code on your host** — textbook RCE class. If you must self-host: enable Settings → Actions → Fork PRs → "Require approval for outside collaborators", never pair `pull_request_target` with an `actions/checkout` of the PR head, and treat the runner as public-untrusted.

## Token-efficiency toggles

Three independent flags in `.bureau.json` `agents.*` — all default OFF. Concept: [`docs/token-efficiency.md`](token-efficiency.md). Full schema: [`docs/configuration.md`](configuration.md).

| Flag | Effect (one-line) |
|---|---|
| `use_goal_loop: true` | implement-pipeline drives via `/goal` (Haiku evaluates per turn) instead of the bash for-loop. Retires the EXP-573 / EXP-571 / EXP-624 / EXP-627 stuck-detector lineage. Requires Claude Code ≥ 2.1.139. |
| `headroom_wrap: true` | Prefix `headroom wrap` on every `claude` invocation. Requires `pip install "headroom-ai[all]"` on the host. |
| `caveman_level: "full"` | Compresses review-prose output ~65%. Installs JuliusBrussee/skills at /bureau-init time; scoped to review stages only — commits + PR bodies stay normal. |

Flip live; no script regen needed. Rollback = set the flag back.

## Status

```bash
scripts/bureau-status.sh            # board state
scripts/bureau-status.sh --cost     # per-stage cost (if cost_tracking enabled)
```
