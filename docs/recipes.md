# Recipes

Common config patterns. Drop these into `.bureau.json` or your launch command as needed.

---

## Single-flight pipeline

Process exactly one issue end-to-end before another enters Spec. Eliminates cross-issue rebase storms in active repos.

```json
{
  "agents": {
    "max_concurrent_issues": 1
  }
}
```

**When to use:** repos with > 3 simultaneous feature tickets where `origin/main` advances faster than the pipeline can drain. Symptom: every cron tick re-runs `merge_origin_main_or_abort` against a different main, conflicts pile up.

**When not to:** small repos where parallelism actually helps. The cap is a knob, not a default.

A single stuck issue (e.g. `needs-human` parking) does *not* deadlock the cap — `count_in_flight_issues` excludes parked issues.

---

## Mixed models per stage

Use a strong reasoning model for spec, a fast cheap model for spec-review, and an *adversarial* model for code-review (catches what the implementer missed).

```json
{
  "agents": {
    "model": "claude-sonnet-4-6",
    "spec":         { "model": "claude-opus-4-7" },
    "spec_review":  { "model": "claude-haiku-4-5-20251001" },
    "implement":    { "model": "claude-opus-4-7" },
    "code_review":  { "model": "claude-haiku-4-5-20251001" }
  }
}
```

Resolution: per-stage > `agents.model` > CLI default. Absent keys fall through.

**Provider mixing.** Anthropic models with reasoning enabled require other Anthropic models in the same context. Bureau is fine — every pipeline run is a fresh subprocess. Cross-provider stage assignment works as long as each stage's full conversation stays within its provider.

---

## Dry-run

Validate a fresh checkout against a real Linear team without polluting state. No Linear mutations, no comments, no PRs, no `git push`.

```sh
BUREAU_DRY_RUN=1 ./scripts/queue-loop.sh implement 1
```

Or pass the flag directly:

```sh
./scripts/queue-loop.sh all 5 --dry-run
```

The pipeline runs to completion — Claude prompts execute, files change in the worktree — but every mutation logs `DRY-RUN:` instead of hitting the network. Useful for:

- Smoke-testing a new agent config before turning it on
- Verifying a model swap (`BUREAU_MODEL_CODE_REVIEW=...`) doesn't break parsing
- Confirming `pick_issue` returns the issue you expect

---

## Monitor escalations

Every `needs-human` event lands as one tab-separated line in `logs/escalations.log`. Tail it from anywhere:

```sh
tail -F logs/escalations.log
```

Line format:

```
2026-05-13T19:18:23Z<TAB>ESCALATED<TAB>EXP-402<TAB>code-review<TAB>cycle=3<TAB>reason="REQUEST_CHANGES exceeded max_review_cycles"<TAB>pr=56<TAB>branch=049-parliament-debate
```

Filter to today's escalations from a specific pipeline:

```sh
grep ESCALATED logs/escalations.log | grep "$(date -u +%Y-%m-%d)" | grep code-review
```

The matching JSON event also fires to `logs/events.jsonl` (so `/bureau-learnings` picks escalations up automatically). The TSV exists for monitors that prefer flat-text grep over jq.

Hooks fire at five sites: code-review (cycle-limit / merge-fail-after-approve / BLOCK), qa (NEEDS_HUMAN verdict), implement (retry-loop terminal). Logs only on `add_issue_label` success — Linear API hiccups don't produce phantom escalations.

---

## Tune the implement retry budget

`implement-pipeline.sh` runs up to `BUREAU_IMPL_MAX_ITER` Claude passes per cron tick (default 3), each capped at `BUREAU_IMPL_ITER_TIMEOUT` seconds (default 1800), with `BUREAU_IMPL_TOTAL_TIMEOUT` (default 5400) bounding cumulative cost.

Aggressive (small batches, fast feedback):

```sh
BUREAU_IMPL_MAX_ITER=2
BUREAU_IMPL_ITER_TIMEOUT=600
BUREAU_IMPL_TOTAL_TIMEOUT=1800
```

Patient (long-running specs, fewer escalations):

```sh
BUREAU_IMPL_MAX_ITER=5
BUREAU_IMPL_ITER_TIMEOUT=2400
BUREAU_IMPL_TOTAL_TIMEOUT=10800
```

Single-strike stuck detection (no `[X]` marks AND no review fixes AND no commits in an iter ⇒ park) is independent of these knobs — Claude spinning without progress always bails fast regardless of remaining iterations.

On macOS the per-iter cap requires `brew install coreutils` (provides `gtimeout`). Without it the script falls back to cumulative-only with a WARN.

---

## Token-efficiency stack

Three opt-in layers that compose: `/goal` (control flow), caveman (output style), Headroom (input compression). Each is independent — enable in priority order, measure, then add the next. Full concept docs: `docs/token-efficiency.md`. Flag schema: `docs/configuration.md`.

**Recommended ramp:**

1. **`use_goal_loop: true` first.** Smallest behavioural surface — replaces the implement-pipeline retry loop with Claude Code's native `/goal` slash command. No new tools to install. Closes the EXP-573 / EXP-571 / EXP-624 / EXP-627 stuck-detector lineage structurally. Run an EXP-621-shape ticket end-to-end and confirm `status=COMPLETE → Build Review` happens via `/goal` rather than the bash loop.

   ```sh
   jq '.agents.use_goal_loop = true' .bureau.json | sponge .bureau.json
   ```

   Requires Claude Code v2.1.139+ on the host. Verify with `claude --version`. On older builds the slash command is a no-op and the pipeline silently falls back through the existing iter loop — broken, not catastrophic, but worth catching early.

2. **`caveman_level: "full"` next.** Compresses output and shrinks the per-session CLAUDE.md load. Trigger an install:

   ```sh
   jq '.agents.caveman_level = "full"' .bureau.json | sponge .bureau.json
   # On the next /bureau-init or --resync-scripts, Phase 6e runs:
   #   npx skills@latest add JuliusBrussee/skills
   #   /caveman-compress CLAUDE.md
   ```

   Scoped to review prose only — commit messages and PR bodies are unaffected. Levels: `lite` (drop filler), `full` (default), `ultra` (telegraphic), `wenyan` (classical Chinese — only enable in repos with Chinese-reading reviewers).

3. **`headroom_wrap: true` last.** Biggest impact (60-95% input reduction on tool-output-heavy stages), biggest surface — wraps the `claude` binary itself via `headroom wrap`. Install Headroom first:

   ```sh
   pip install "headroom-ai[all]"
   headroom --version    # confirm
   jq '.agents.headroom_wrap = true' .bureau.json | sponge .bureau.json
   ```

   The proxy isn't started — `headroom wrap` handles its own process management per invocation. CacheAligner stabilizes prompt-cache prefixes so per-iter calls actually hit the Anthropic cache (the pre-flag dynamic context shifted enough turn-to-turn that most prompt-cache hits were missed).

Rollback at any layer: flip the flag back to `false` / `"off"` in `.bureau.json` and the pipeline reverts on the next tick — no script changes, no state migration.

---

## Telegram alerts

Get a message when a pipeline fails. No-op when the credentials aren't set, so dev environments stay quiet.

In the target repo's `.env`:

```sh
TELEGRAM_BOT_TOKEN=123456:ABC-XYZ
TELEGRAM_ALERT_CHAT_ID=-100123456789
```

Throttling: max 1 alert per `(issue, class)` per hour, tracked at `/tmp/bureau-alerts.log`. Bypass by deleting the file.

See [exit codes](exit-codes.md) for the full alert classification.

---

## Multi-repo, side-by-side

Each repo gets its own tmux session, scoped by folder name. Run unlimited pipelines in parallel:

```sh
cd ~/projects/sofa          && ./scripts/start-bureau-v2.sh   # → bureau-v2-sofa
cd ~/projects/brainhuggers  && ./scripts/start-bureau-v2.sh   # → bureau-v2-brainhuggers
tmux ls | grep bureau-v2-                                      # both listed
```

**Caveat:** the Linear API key is per-user across all repos. Make sure each repo's `.bureau.json` points at a *different Linear project* — otherwise two workers race on the same issues.

---

## Override the session name

```sh
BUREAU_SESSION_NAME=nightshift ./scripts/start-bureau-v2.sh
tmux attach -t nightshift
```

Useful when you want a single named session you can find without remembering the basename.

---

## Sampling-mode threshold for code-review

Repos with mature CI / type-safety can review larger diffs exhaustively. Repos without CI need stricter sampling.

```json
{
  "agents": {
    "code_review_sampling_threshold": 1000
  }
}
```

Default is `500` lines. Above this, code-review specialists shift to sampling-by-class. Tune up for confidence in tooling, down for legacy code.

---

## Custom merge strategy

Repos with linear-history-via-rebase policies should opt out of squash:

```json
{
  "agents": {
    "merge_strategy": "rebase"
  }
}
```

Valid values: `squash` (default), `merge`, `rebase`. Anything else falls back to `squash` with a warning.

---

## Tighter supervisor

For long-running headless deployments, raise the give-up threshold so a transient flaky tick doesn't kill the pipeline:

```json
{
  "supervisor": {
    "max_crashes": 10,
    "stability_window": 7200
  }
}
```

Or override at launch:

```sh
BUREAU_SUPERVISOR_MAX_CRASHES=10 ./scripts/start-bureau-v2.sh
```

---

## Memory loop (logs → `LESSONS.md`)

Mine the pipeline's own runs for recurring failure modes and review-feedback patterns, then feed a human-curated summary back into future spec + code-review prompts.

### How it works

1. **`queue-loop.sh` emits events.** Every time the queue picks a real candidate, `run_script` writes two JSONL lines to `logs/events.jsonl` — one `stage_start`, one `stage_end` with `issue`, `branch`, `exit_code`, `class`, `duration_s`. Queue-empty ticks emit nothing.

   ```json
   {"ts":"2026-05-11T14:23:01Z","event":"stage_end","mode":"all","stage":"code-review-pipeline.sh","issue":"EXP-512","branch":"037-foo","exit_code":14,"class":"build-failed","duration_s":287}
   ```

   `logs/` is gitignored. `events.jsonl` is append-only — truncate it manually if it grows large; there's no auto-rotation in v1.

2. **`/bureau-learnings` drafts `LESSONS.md`.** After a week of pipeline activity, run the slash command in the target repo. It:
   - Filters events to the last 30 days.
   - Pulls Linear comments for failed and successfully-reviewed issues (via the Linear MCP).
   - Clusters failure modes by `(class, first-file-in-trace)`, review feedback by repeated 4–8-word n-grams.
   - Requires ≥3 distinct issues per finding — empty sections are explicitly labeled "below threshold," **not** filled with fabricated patterns.
   - Writes a draft `LESSONS.md` at the repo root with three sections (failure modes / review feedback / stage timing p50/p90).
   - **Never stages, commits, or pushes.**

3. **You curate.** Review the diff, delete bullets you disagree with, edit the wording, then `git add LESSONS.md && git commit`. Anything you dismiss will be re-proposed by future runs if the pattern persists — that's a feature, not a bug.

4. **Pipelines read it back, advisory only.** `bureau-config.sh::build_lessons_context` reads `LESSONS.md` from cwd (the worktree root) and wraps it with a "Treat as advisory, not binding" preamble. The result is injected into:
   - `spec-pipeline.sh` Phase 1 (`speckit-specify`) — where decisions are first shaped.
   - `code-review-pipeline.sh` — into all three specialist prompts (correctness, security, performance).

   Other stages (`ux`, `copy`, `qa`, `merge`, `rebase`) deliberately do **not** include it. If the file is absent or whitespace-only, injection is a no-op — pipelines run unchanged.

### Why human-in-the-loop (not auto-applied)

Low-volume early data produces noisy clusters. Auto-applying would amplify garbage. Human curation gates the feedback loop and keeps signal high. There is no vector DB, no embeddings, no external LLM in this path — just `jq` over `events.jsonl` plus Linear MCP comment lookups.

### Schema fields

| Field | Type | Notes |
|---|---|---|
| `ts` | string | ISO-8601 UTC, auto-injected |
| `event` | string | `stage_start` \| `stage_end` |
| `mode` | string | `queue-loop.sh` mode (`spec`, `implement`, `all`, …) |
| `stage` | string | Pipeline script name (e.g. `spec-pipeline.sh`) |
| `issue` | string | Linear identifier (e.g. `EXP-512`) |
| `branch` | string | Spec branch from `<!-- bureau-branch: -->` marker (omitted if not yet resolved) |
| `exit_code` | number | Pipeline exit status (only on `stage_end`) |
| `class` | string | Mapping per [`docs/exit-codes.md`](exit-codes.md): `ok`, `queue-empty`, `linear-down`, `build-failed`, … |
| `duration_s` | number | Wall-time seconds between start and end (only on `stage_end`) |

To add your own fields from a pipeline script, call `emit_event "event=..." "key=value" ...` — values matching `^-?[0-9]+$` are auto-typed as JSON numbers. The helper is silent-on-failure (any error logs to stderr and returns 0) so it can never wedge a cron-driven pipeline.

---

## See also

- [Configuration](configuration.md) — full reference
- [Exit codes](exit-codes.md) — what each failure means
- [Troubleshooting](troubleshooting.md) — when things break
