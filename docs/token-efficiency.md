# Token efficiency: `/goal`, caveman, headroom

Three opt-in layers that compose. Each addresses a different axis of pipeline cost:

| Layer | Axis | What it changes | Default state |
|---|---|---|---|
| `/goal` | **Control flow** | Replaces the implement-pipeline bash retry loop with Claude Code's native `/goal` slash command. Haiku evaluates the goal condition after every turn. | OFF |
| caveman | **Output** | Skill that compresses Claude's reply style (review prose, status comments). ~65% average output reduction; code/file paths/error strings stay byte-exact. | OFF |
| Headroom | **Input** | Wraps the `claude` binary so prompts and tool outputs are compressed before they hit the LLM. 60-95% input reduction on tool-output-heavy stages. Reversible via the `headroom_retrieve` MCP tool. | OFF |

This page is the concept-level explainer. The flag schema is in [`docs/configuration.md`](configuration.md#token-efficiency-flags-bureaujson-agents). The recommended adoption ramp is in [`docs/recipes.md`](recipes.md#token-efficiency-stack).

---

## Why now

Bureau's `implement-pipeline.sh` carried a hand-rolled retry loop (~170 lines) that was the source of four consecutive stuck-detector bugs through the EXP-573 → EXP-571 → EXP-624 → EXP-627 lineage. Each fix narrowed the detector; none addressed the root cause that we were hand-implementing what `/goal` provides natively.

Around the same time, two off-the-shelf primitives (caveman and Headroom) matured to production-ready quality. They compose with each other and with `/goal`. Adopted together they cut roughly **80% of tokens per pipeline tick** while removing the most accident-prone bash in the repo.

---

## Layer 1 — `/goal`

**What it is.** A session-scoped slash command in Claude Code v2.1.139+ that sets a completion condition. After every turn, a small fast model (Haiku) reads the transcript and decides whether the condition holds. If not, Claude starts another turn; if yes, the session ends.

**What it replaces.** The implement-pipeline iter loop. Pre-flag: bash runs a `for i in 1..MAX_ITER` loop, invokes `$CLAUDE` with a long prompt each iter, parses a self-reported JSON status block, runs a "stuck detector" that counts commits + tasks_done, and decides continue/stop. Post-flag: bash invokes `$CLAUDE -p "/goal CONDITION"` once; the iter loop runs *inside* Claude's session under Haiku's supervision.

**What changes for agents.** The work instructions (read tasks.md, implement, commit, mark `[X]`) move into `--append-system-prompt`. The verification surface (every `[ ]` becomes `[X]`, fenced JSON status block reports COMPLETE) moves into the `/goal CONDITION`. The agent's job is unchanged; the harness that drives it is different.

**What stops working.** The single-strike stuck detector. The post-loop EXP-571/EXP-624 override. `BUREAU_IMPL_ITER_TIMEOUT` (turns end when Claude returns, not on a wall-time cap per turn). All deliberate — the goal evaluator does the same checks structurally.

**What still works.** Terminal STATUS parsing (Claude emits the same fenced JSON block on the final turn). PR creation / state moves / `[skip ci]` amend / EXP-622 ready-flip / EXP-628/629/631 upstream-port. The exit-code mapping in `docs/exit-codes.md` is unchanged. `BUREAU_IMPL_MAX_ITER` becomes "stop after N turns" inside the goal condition, not a bash for-loop bound — same upper bound, different mechanism.

**Cost story.** Haiku evaluator tokens are negligible compared to main-turn spend (per Anthropic's docs). Main saving is removed scaffolding + retired class of stuck-detector bugs.

**Failure modes.**

- Claude Code older than 2.1.139 doesn't recognize `/goal` — the slash command is treated as raw text and the iter-loop replacement silently doesn't happen. Probe `claude --version` before flipping the flag.
- A long goal condition (>4000 chars) is rejected. Keep the condition tight; put work instructions in `--append-system-prompt`.
- Haiku misjudges a condition. Mitigated by the branch-ahead commit-count backstop — `STATUS=COMPLETE` + 0 commits beyond `origin/main` still flips to STUCK.

---

## Layer 2 — caveman

**What it is.** A Claude Code skill (`/caveman <level>`) that strips ceremony from Claude's reply style. Four levels: `lite` (drop filler), `full` (default caveman), `ultra` (telegraphic), `wenyan` (classical Chinese). Code, file paths, function names, and error strings stay byte-exact regardless of level.

**Why "caveman".** The compressed style reads like a caveman speaking ("New object ref each render. Wrap in `useMemo`."). The README's pitch: "why use many token when few do trick." See [JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman) for the full pitch and 65% output-reduction benchmark on Claude API.

**What it replaces.** Verbose review-comment prose. The pre-flag code-review-pipeline output ("Sure! I'd be happy to help with that. The issue you're experiencing is most likely caused by…") becomes ("Bug in auth middleware. Token expiry check use `<` not `<=`. Fix:"). Same fix, 75% fewer tokens.

**What changes for agents.** When the per-stage prompt prefix `/caveman <level>` is detected, prefer telegraphic sentences. Drop "I'd be happy to", "Let me take a look at", "The reason this is happening is". Code blocks and identifiers stay verbatim.

**What stops working.** Verbose review prose. Long-form architectural explanations during review. Both intended trade-offs — the operator-facing artifacts (commit messages, PR titles, PR bodies) are deliberately excluded from caveman so humans can still read them.

**What still works.** Implement-pipeline (no caveman prefix applied — full prose for tasks.md edits, normal commit messages). QA pipeline. Merge pipeline. Upstream-port. All operator-facing comms.

**Cost story.** Caveman only affects output tokens. The Anthropic API charges 5× more for output than input, so a 65% cut on review comments is a meaningful share of the budget. The included `caveman-compress` subcommand also rewrites CLAUDE.md memory files (~46% reduction every session start) — that's an input-side win on top.

**Failure modes.**

- Caveman style leaking into commit messages or PR bodies — see [troubleshooting](troubleshooting.md#caveman-style-leaked-into-a-commit-message-or-pr-body).
- A reviewer who can't tolerate telegraphic style. Level `lite` is the soft option; flip `caveman_level` back to `off` if even `lite` is too aggressive.

---

## Layer 3 — Headroom

**What it is.** A library / proxy / CLI wrapper ([headroomlabs-ai/headroom](https://github.com/headroomlabs-ai/headroom)) that compresses everything an LLM agent reads — prompts, tool outputs, file reads, RAG chunks, conversation history — before it reaches the model. Reversible: originals are cached, Claude calls `headroom_retrieve` to pull them back when a summary is too lossy.

**What it replaces.** The pre-flag claude_cmd_for_stage emitted `claude -p --print --dangerously-skip-permissions [--model M]`. Post-flag: `headroom wrap claude -p --print --dangerously-skip-permissions [--model M]`. Same downstream contract, with Headroom intercepting the request envelope.

**What changes for agents.** Tool outputs and file reads may be summarized. The `Read` tool's output for a long file may carry a summary instead of full content. If a summary is missing a specific line range, function body, or stack frame needed for the task, call the `headroom_retrieve` tool to fetch the original. Don't hallucinate around a summary — the original is one tool call away.

**What stops working.** Implicit assumption that the user prompt is byte-identical to what bash constructed. The system prompt and the goal condition are not compressed; tool outputs and history are. Most agent code is unaffected; the cases that break are ones that grep for specific substrings in tool output without first asking for the original.

**What still works.** All `--model` resolution (Headroom forwards flags). All bureau-config helpers (Linear glue, post_comment, etc.). The codex path in `claude_cmd_for_stage` is left alone — codex has its own sandboxing layer that doesn't compose cleanly with `headroom wrap`. The CacheAligner sub-component stabilizes prompt-cache prefixes so per-iter calls actually hit the Anthropic prompt cache (the pre-flag dynamic context shifted enough turn-to-turn that most cache hits were missed).

**Cost story.** 60-95% input-token reduction on tool-output-heavy stages (qa-pipeline test output, code-review-pipeline diff reads, upstream-port full upstream diff). CacheAligner adds further savings when per-iter prompt-cache hit rate goes up. Reported benchmarks: SRE incident debugging 92%, GitHub issue triage 73%, codebase exploration 47%.

**Failure modes.**

- `headroom` not on PATH after `headroom_wrap: true`. Install via `pip install "headroom-ai[all]"`. The first stage invocation surfaces the error loudly.
- Summary too lossy for the current task and the agent doesn't call `headroom_retrieve`. Mitigated by the agent-facing instruction block in the generated CLAUDE.md (SKILL.md Phase 6a) — agents are told to ask for the original rather than hallucinate.

---

## Composition order

The layers are independent. Recommended ramp (from [recipes.md](recipes.md#token-efficiency-stack)):

1. **`/goal` first.** Smallest behavioural surface — no new install. Removes the stuck-detector tangle.
2. **caveman next.** Quick output-side win. Doesn't touch control flow.
3. **Headroom last.** Biggest impact and biggest surface. Needs an install. Compose only after the other two are stable.

Each flag is independently flippable. If a layer misbehaves, flip its flag back to `false` / `"off"` and the pipeline reverts on the next tick — no script regeneration, no state migration.

---

## What this stack deliberately doesn't do

- **No application of `/goal` to qa-pipeline or code-review-pipeline.** Neither has an in-process retry loop. `code-review-pipeline.sh`'s `MAX_REVIEW_CYCLES` is a ticket-level counter that increments across separate invocations of the script, not within one.
- **No `headroom learn` integration in the initial PR.** Requires a failure-trace corpus to be useful; deferred until the pipeline has been producing traces under Headroom for a couple of weeks.
- **No cavecrew-* subagent migration.** Deferred until caveman is proven in-context. Adds a parallel subagent path that complicates shepherd.
- **No headroom MCP-server install** (the `headroom_compress` / `headroom_retrieve` / `headroom_stats` MCP tools as standalone). The `wrap claude` mode covers the bureau use case; the MCP tools are for non-bureau MCP clients.
- **No brand-name / symbol-level substitution layer.** Different category from path translation (which lives in `upstream-port.sh` via `.bureau-port-map.json`). String-rewriting whole content risks corrupting docstrings, error messages, and reviewer comments. Stays out of scope.

---

## See also

- [Configuration](configuration.md#token-efficiency-flags-bureaujson-agents) — full flag schema
- [Recipes](recipes.md#token-efficiency-stack) — recommended adoption ramp
- [Troubleshooting](troubleshooting.md#token-efficiency-layers) — failure-mode dispositions
- [Exit codes](exit-codes.md) — note on why the exit-code mapping is unchanged under these flags
- [JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman) — caveman skill repo
- [headroomlabs-ai/headroom](https://github.com/headroomlabs-ai/headroom) — Headroom proxy/wrap repo
- [Anthropic `/goal` docs](https://code.claude.com/docs/en/goal) — upstream reference
