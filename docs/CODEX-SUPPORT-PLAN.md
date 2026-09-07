# Bureau: Codex app and CLI support plan

Historical assessment prepared 2026-09-07 against the public initial snapshot `6763c26`. Findings and line references below describe that baseline. For the implemented behavior and its validation limits, read the [stage guide](stage-protocol.md), [provider guide](provider-runtime.md) and [acceptance record](codex-acceptance.md).

**Recommendation:** retain Bureau's Linear state machine, stage rules, and shell automation; introduce a shared stage interface that can be executed either by the current Codex app task or by a background Claude/Codex CLI worker. Deliver app tasks first, then background parity. Preserve Claude Code support throughout.

## 1. What the repository actually provides

Bureau is a bootstrap skill that installs a workflow into another repository. This repository contains 23 shell templates, including nine stage pipelines, five interactive command templates, one JavaScript scheduling workflow, and the installer instructions in `SKILL.md`. There is no application server or package-managed runtime to port.

The main components are:

| Component | Current responsibility | Migration treatment |
|---|---|---|
| `SKILL.md` | Interactive discovery, configuration, Spec Kit installation, copying templates, validation, resync | Support both hosts and resolve assets relative to the installed skill |
| `bureau-config.sh` | Linear API, runner/model resolution, parsing, worktrees, merge safeguards, events and quotas | Extract stable, host-independent interfaces incrementally |
| Nine stage pipelines | Prepare context, call a model, validate artifacts, mutate Linear/GitHub | Separate preparation, model work, and completion |
| `queue-loop.sh`, supervisor, `shepherd.sh`, `orchestrate.sh` | Polling, recovery, single-ticket and concurrent execution | Reuse for background mode; share coordination with app tasks |
| Five command templates | Interactive queue/spec/build/learnings workflows | Convert to Codex skills while reconciling drift from shell stages |
| `conflict-aware-schedule.js` | Predict file overlap and group tickets | Extract deterministic graph logic from its host-specific execution API |
| Dashboard, tests, documentation | Operations and regression checks | Extend to distinguish hosts, providers, and execution outcomes |

The shared core is worth retaining: direct Linear GraphQL calls, branch-marker lookup, spec artifacts, review cycles, retry limits, merge gates, and the lessons loop all remain useful with Codex.

## 2. Existing Codex support and concrete gaps

**Codex CLI support already exists, but is partial.** Per-stage runner selection can produce a `codex-stage-runner.sh` invocation. That wrapper uses `codex exec` and captures the final message. Current tests verify routing and a warning for build/test stages, rather than proving those stages work end to end. See [runner resolution](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L180), [adapter](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/codex-stage-runner.sh#L61), and [routing tests](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/tests/test_model_resolution.sh#L160).

| Gap | Current evidence | Consequence |
|---|---|---|
| Installation assumes Claude | [SKILL.md:35](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/SKILL.md#L35), [asset paths:91](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/SKILL.md#L91), [launcher:20](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/start-bureau-v2.sh#L20) | Codex-only setup still requires Claude and resolves templates from its installation directory |
| Instructions and skills assume Claude | [Spec Kit init:468](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/SKILL.md#L468), [spec prompt:182](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/spec-pipeline.sh#L182), [instruction context:1538](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L1538) | No root `AGENTS.md`; generated instructions and spec paths are not host-aware |
| Interactive commands differ from the worker protocol | [spec branch comment:119](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/commands/linear-to-spec.md#L119), [branch fallback:39](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/commands/linear-implement.md#L39), [Review state:146](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/commands/linear-implement.md#L146) | Commands do not require the canonical branch marker and retain hardcoded branch/team/state assumptions; copying them alone would perpetuate divergence |
| Authentication is not runner-aware | [auth helper:1324](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L1324), [shepherd:167](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/shepherd.sh#L167) | Spec/shepherd probe Claude even for Codex runs; a missing executable can pass the text-only error check |
| Codex model configuration is separate and incomplete | [model resolution:233](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L233) | `.agents.<stage>.model` is ignored for Codex; only Codex-specific environment variables are used |
| Goal mode is incompatible with the adapter | [goal invocation:385](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/implement-pipeline.sh#L385), [adapter prompt:51](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/codex-stage-runner.sh#L51) | With the generated command, the adapter receives `--append-system-prompt` as its prompt and drops the real instructions |
| Stage permissions do not match actual work | [read-only selection:223](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L223), [spec-review edits:116](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/spec-review-pipeline.sh#L116) | Spec review is configured read-only although its prompt requires edits and commits |
| Structured output is not normalized | [parser:1590](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L1590) | Bare schema-valid JSON is ignored; most stages also combine stderr and stdout, undermining the adapter's clean-output contract |
| Worktree management assumes disposable workers | [detach helper:1228](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L1228), [reset helper:1255](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L1255) | Bureau can detach another task's checkout; hard reset and `clean -fdx` must never be applied to an app-owned worktree |
| Local configuration is not worktree-portable | [config lookup:5](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L5), [gitignore setup:873](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/SKILL.md#L873) | `.bureau.json` and `.env` are ignored; a newly created app worktree does not automatically acquire them |
| Commit ownership assumes Claude | [ownership check:637](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L637), [spec attribution:279](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/spec-pipeline.sh#L279) | Codex commits can be classified as human and refused by automatic rebase, or receive incorrect Claude attribution |
| Review can merge outside the dedicated gate | [inline merge:402](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/code-review-pipeline.sh#L402), [shepherd stop:297](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/shepherd.sh#L297) | Disabling the merge agent means review may merge directly; `--no-merge` only checks the Merge state, so it is not a global stop-before-merge guarantee |
| Scheduling and telemetry are host-specific | [workflow](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/workflows/conflict-aware-schedule.js#L1), [quota signals:861](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L861), [costs:340](https://github.com/KaiaK808/bureau/blob/6763c26c26aa96a41a92fbe95416fddbf4d48f69/templates/scripts/bureau-config.sh#L340) | Workflow globals are not an ordinary Node program; Claude usage can throttle Codex, and Codex costs are silently omitted |

The sandbox warning should become a capability check, not a permanent claim that Codex cannot implement or test. Network access is configurable, while Git metadata and existing `.agents`/`.codex` directories are protected under the default workspace-write policy. Setup, commits, tests, and external operations must use the host's permitted execution paths. Moving an operation into a shell helper does not bypass the app's sandbox. [Official permissions documentation](https://learn.chatgpt.com/docs/agent-approvals-security).

## 3. What I verified

- All **23 existing shell tests passed**.
- Bash syntax checks passed for the scripts and test files covered by CI.
- The configured ShellCheck invocation passed for its four selected pipeline/config files.
- Installed Codex CLI: `0.144.1`; its local help confirms stdin prompts, `--output-schema`, `--json`, explicit working directories, and final-message output.
- Installed Spec Kit: `0.7.5`, matching Bureau's pin.
- A temporary-directory spike ran Claude initialization, Codex initialization, then Codex initialization again. Both sets of nine skills remained, both instruction files existed, and a customized constitution survived. The active `.specify/integration.json` became `codex`; coexistence therefore needs an explicit policy for the selected Spec Kit integration.
- Local stub probes reproduced ignored Codex JSON model selection, bare-JSON parse failure, the missing-Claude auth false pass, and the goal-mode prompt loss.

These checks did not invoke a paid model or operate a real Linear/GitHub pipeline. Real Codex stage behavior, target-project tests, connector access, and unattended permissions still need acceptance testing. The existing green suite is a regression baseline, not evidence of full Codex compatibility.

## 4. Proposed operating model

Keep three independent choices explicit:

1. **Installed interface:** Claude Code, Codex, or both.
2. **Execution mode:** current app task or background worker.
3. **Background runner per stage:** Claude CLI or Codex CLI, with provider-specific model settings.

In app mode, the current task performs the work with its available tools and selected model. A Bureau skill should not launch another CLI merely to do the same work inside a hidden session. In background mode, a runner adapter performs model work while the shell driver owns scheduling and transitions.

Both modes use this stage lifecycle:

```text
inspect → claim → prepare → perform work → validate result → complete/release
                    ↑                            ↓
             shared stage prompts       shared transition rules

perform work = current Codex task OR Claude/Codex CLI adapter
```

Add a small script interface for inspection, claims, prepared context, and completion. Names such as `bureau.sh prepare` and `bureau.sh finish` are proposed interfaces, not existing commands. Extract current behavior behind that interface stage by stage; the established pipeline entry points remain wrappers around it.

Prepared context identifies the issue, stage, source revision, canonical branch, artifact paths, constraints, and expected result schema. Completion checks the claim, expected state/revision, result structure, and objective evidence before applying transitions. Repeated completion must not duplicate comments, sub-issues, or PRs.

Linear remains authoritative for workflow state. A small run record supplies resumability: run ID, owner, stage, branch, source/result SHAs, evidence paths, and outcome. Store operational records outside disposable worktrees in an explicitly configured, permitted location; do not introduce a database service.

## 5. Implementation sequence

### PR 1 — Portable installation and project instructions

**Outcome:** Bureau and Spec Kit are discoverable from both assistants.

- Add `AGENTS.md` for maintaining this repository, carrying the relevant invariants from `CLAUDE.md`. Keep one shared source for common guidance.
- Split the long installer prompt into a concise entry point and focused references. Resolve template/document paths from the installed skill, including symlinked installations.
- Add target selection and conditional prerequisites: app mode must not require Claude or tmux; background mode checks the selected CLIs.
- Generate Codex skills for all five Bureau commands with proper `name`/`description` metadata. Replace Claude's `Skill` tool assumptions and literal argument placeholders with instructions appropriate to the host.
- Generate managed Bureau sections in `AGENTS.md` and `CLAUDE.md` without duplicating or overwriting project guidance.
- Use the already verified `specify init --here --integration codex --force --no-git --ignore-agent-tools` path for Codex. Preserve the pin, constitution, manifests, and customized files. Explicitly define the active integration when both are installed.
- Extend update/resync to cover installed interfaces and newly introduced prompt/schema assets. Preserve custom files and make drift reviewable.

Codex's documented repository skill location is `.agents/skills`; symlinked skill folders are supported. A personal installation can be exposed through `~/.agents/skills`. A distributable plugin can follow later; it is not required for the first app release. [Skills documentation](https://learn.chatgpt.com/docs/build-skills). Project guidance should use `AGENTS.md`. [Instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

**Acceptance:** fresh Claude-only, Codex-only, and dual installations; repeat installation; resync with local modifications; installation paths containing spaces; user instructions and constitutions preserved.

### PR 2 — Shared stages and safe coexistence

**Outcome:** an app task and a background worker can participate without conflicting state or checkout ownership.

- Extract prompt/context construction, result validation, and transition handling from the pipelines. Start with spec and implementation, then review/QA; retain optional UX/copy/research behavior.
- Reconcile the five command workflows with the canonical branch marker and configured states. Preserve any intentionally separate interactive sub-issue behavior, with stable task identifiers and idempotent creation.
- Implement explicit current-workspace and disposable-worker modes. Never reset, clean, or detach an app/user-owned checkout. If another owner holds the desired branch, report the conflict and support a controlled handoff.
- Add local per-issue and per-worktree locking shared by every entry point, with owner/run identity and recovery after interruption. A Linear label alone is not an atomic lock. Multi-machine coordination remains a separate feature unless required.
- Make config discovery explicit and worktree-aware; honor an explicit config path instead of resetting it. Supply ignored environment/configuration through a setup helper without committing secrets.
- Introduce a Bureau ownership marker independent of model identity, preserving recognition of legacy commits. Keep human-commit protection conservative; include shell-generated checkpoint commits in ownership tests.
- Route all automatic merging through the existing complete gate set. Propagate stop-before-merge to every entry point and make it independent of whether a Merge state exists.
- Separate inspection from mutation. Existing shepherd dry-run processing reaches auth and `--from-stage` handling before its dry-run branch; the new inspection interface must have no writes or model calls.

Codex app worktrees can start detached and live outside the repository's `.worktrees` directory, so current-directory assumptions need explicit handling. [Worktree documentation](https://learn.chatgpt.com/docs/environments/git-worktrees).

**Acceptance:** competing starts have one owner; app changes survive background ticks; interrupted runs can resume; stale results cannot advance state; repeated completion is idempotent; no-merge works with the merge agent enabled and disabled; existing merge-gate regressions remain green.

### PR 3 — First complete workflow inside the app

**Outcome:** open an adopting repository and drive one ticket through spec, implementation, QA, and review in the current task.

- Add a Codex operator skill that uses the shared stage interface, with direct stage commands for spec, implementation, review, status, and resume.
- Use the current task for coding and investigation. Permit explicitly requested specialist subagents for review; create separate sidebar tasks only when the user requests them.
- Preserve the configured workflow boundaries and previously granted authorization. A request to prepare/review work must stop at that boundary; a request to run the full authorized workflow can continue through it.
- Show changed files, test evidence, blockers, and PR links in the app. Open the relevant diff or artifact where available.
- Add app environment setup and convenient actions for status, validation, and tests. Generate configuration through the supported app flow; do not overwrite unrelated `.codex` settings.
- Use available Linear tools for interactive discovery where appropriate, with the established API-key path as fallback. Keep automated Linear transitions behind shared helpers. Inspect actual connector/tool availability rather than hardcoding Claude MCP server names.
- Persist enough state to resume after stopping the task or handing work between the app and a background worker.

The app supports project setup scripts, terminal actions, and Git/diff controls. These complement the shared scripts. [Local environments](https://learn.chatgpt.com/docs/environments/local-environment).

**Acceptance:** a representative ticket completes through review entirely in an app task, without spawning Claude or requiring tmux; another ticket resumes from an existing branch; a blocked task retains its progress and explanation. This is the first user-facing release milestone.

### PR 4 — Full Codex CLI and mixed-runner execution

**Outcome:** existing queue/shepherd/orchestrate entry points can run all configured creative stages with Codex.

- Replace command strings with a quoted runner function/argv interface. Support prompts through files/stdin, explicit working directory, model, reasoning effort, permissions, schema, timeout, cancellation, and separate result/log streams.
- Preserve existing Claude configuration behavior. Add validated Codex configuration in JSON plus environment overrides, documenting precedence and rejecting unknown runners instead of silently choosing Claude.
- Add runner-specific executable/authentication checks before state mutation. Use non-generative status checks where supported; distinguish auth, quota, environment, timeout, malformed output, and code/test failures.
- Normalize Claude text/envelopes and Codex structured output into one validated stage result. Keep diagnostics separate so log text cannot become a verdict. Retain raw evidence for failed runs.
- Gate implementation and QA by actual host capabilities. In background mode, let the deterministic executor handle permitted Git/CI operations and independent test verification. In app mode, those operations remain subject to app permissions.
- Make spec-review permissions match its edits, or split diagnosis and repair explicitly.
- Keep the existing bounded implementation loop as the portable default. Treat Claude `/goal` and Headroom as Claude capabilities; do not send Claude CLI flags to Codex. A later Codex goal integration needs its own verified contract.
- Remove residual direct Claude invocations in launchers and optional upstream-port summarization. Select or omit the interactive tmux bench according to configuration.

Codex supports schema-constrained final results and JSONL events in non-interactive mode. [Non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode).

**Acceptance:** fake executors exercise success, failure, timeout, cancellation, large prompts, and malformed results; then run representative real tickets in Codex-only and mixed-provider configurations. Missing Claude must not affect a Codex-only run. A permissions failure must not be reported as an implementation defect.

### PR 5 — Background supervision, scheduling, and telemetry

**Outcome:** supervise ongoing work from the app while retaining terminal-based operation.

- Extract one bounded queue tick from `queue-loop.sh`; scheduled app runs must not start an infinite loop on every invocation.
- Retain tmux/supervisor support for existing operators. Add app follow-ups using native scheduling when the user requests monitoring or recurrence; notify only on meaningful change, completion, failure, or required action.
- Make run outcomes explicit: waiting, blocked, stopped for review, failed, completed. A successful process exit must not by itself mean the ticket reached Done.
- Extract the scheduler's pure file-overlap grouping into testable code. Keep the current workflow wrapper and add app/CLI ways to obtain predictions. Failed/missing predictions must remain visible instead of silently removing tickets from a schedule.
- Preserve concurrency caps and enforce the shared ownership rules across app and CLI entry points.
- Record provider, model when known, stage, run ID, timing, usage, and result. Keep account usage, estimated cost, and actual billed cost distinct. Show unavailable metrics as unavailable.
- Separate Claude and Codex quota signals. Use supported app usage tools for app supervision; do not depend on scraping undocumented account files for background Codex limits.

App scheduling supports follow-ups in the current task and independent scheduled runs. Local-file tasks require the computer on and the app running, so this mode is not a replacement for an always-on host. [Scheduling documentation](https://learn.chatgpt.com/docs/automations?surface=app).

**Acceptance:** no duplicate workers after repeated wakeups; pause/resume works; unchanged states stay quiet; Codex is not paused because Claude quota is high; a halted ticket is not reported as completed.

### PR 6 — Migration, release checks, and documentation

**Outcome:** existing Claude users can opt into Codex without recreating their workflow.

- Add an additive config migration with an explicit version and preview. Preserve legacy booleans/string agent settings, model precedence, state IDs, labels, and customized scripts.
- Add a doctor/status command reporting installed interfaces, effective runners/models, relevant capabilities, missing assets, active Spec Kit integration, and config provenance.
- Extend tests to use fake Claude and fake Codex through real production parsing/runner helpers. The current copied parser in the test stub already differs from production; avoid carrying that divergence forward.
- Cover Bash 3.2/macOS and Linux; include a representative application whose tests need dependencies or a local server, not just Bureau's shell fixtures.
- Update README, configuration/exit-code references, operator cheatsheet, recipes, installer appendices, and HTML documentation together. Add app-first quickstart and mixed-runner recipes.
- Document rollback: switch the background runner to Claude and retain artifacts/state. Installing a second interface should not require deleting the first.

**Acceptance:** Claude-only regression run, app workflow, Codex-only headless run, mixed-runner run, installation/resync matrix, interruption recovery, concurrency checks, and stop-before-merge checks all pass. Only then advertise full Codex support.

## 6. Delivery boundaries and remaining decisions

PRs 1–3 deliver the requested app-first experience. PR 4 adds full background execution; PRs 5–6 complete supervision and migration. Tests ship with each PR, not only at the end. This is a medium-sized refactor touching the installer and most stage entry points, rather than a documentation-only port.

Use one representative target repository for acceptance before expanding to other stacks. Keep merge behavior explicit per run and preserve the existing strict gates. Leave exact Codex model choices configurable and respect the app's selected model in current-task mode.

Remaining validation is concrete: app permissions for installation/Git operations, actual Linear tool access, target-project test requirements, full Spec Kit workflow behavior after switching integrations, and provider usage signals available to the background host. None of these requires replacing Linear, Spec Kit, or Bureau's shell architecture.
