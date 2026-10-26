# 0001 Isolated stages with a shared provider and exit protocol

- **Status**: accepted (derived)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `7ebaf8a` (provider and shared-stage implementation, released in v2.0.0); `docs/release-notes-v2.0.0.md:5` (app and background modes); `docs/stage-protocol.md:3` (workspace modes and ownership); `docs/provider-runtime.md:3` (adapter boundary); `docs/provider-runtime.md:31` (authentication, termination and exits); `templates/scripts/bureau-config.sh:728` (provider invocation); `templates/scripts/bureau-config.sh:3578` (review verdict exits); `templates/scripts/bureau-config.sh:3596` (shepherd exit policy); `templates/scripts/shepherd.sh:893` (quiet requested-stop exception); `templates/scripts/bureau-config.sh:3605` (current exit vocabulary).

## Context

Bureau installs a runtime into adopting repositories. Specification, implementation, QA and review have different inputs, write permissions and completion rules; sharing a conversational process would make those boundaries implicit. The v2.0.0 release established separate background execution and a current-task app protocol.

## Options considered

- One long provider session for the whole workflow reduces startup overhead but couples context, permissions and recovery across stages.
- Independent stage calls through a common executor add startup and coordination work but expose stage identity, bounded execution and explicit outcomes.
- Giving each provider its own routing and Git machinery simplifies its adapter initially but duplicates authorization and failure handling.

## Decision

Run background creative work through separate headless provider invocations using run_stage_for and bureau-provider.py; keep provider argv construction, stdin prompts, authentication, final-result parsing and timeout/cancellation handling behind that adapter. Sources: `7ebaf8a`; `docs/provider-runtime.md:3`; `templates/scripts/bureau-config.sh:728`.

Run background stages only in explicitly registered disposable workers after claiming issue and canonical workspace ownership; app stages instead use prepare/finish in their current checkout. A worktree is an execution boundary, not a promise of a newly allocated directory for every stage or provider call. Sources: `docs/stage-protocol.md:3`; `docs/release-notes-v2.0.0.md:5`.

Treat exits as a shared protocol: authentication is 16, provider/result error 22, quota wait 23, environment block 24, timeout 124 and cancellation 130. Routing belongs to the deterministic executor, and provider diagnostics cannot supply a verdict. Sources: `docs/provider-runtime.md:31`; `templates/scripts/bureau-config.sh:3605`.

Have the shepherd continue on 0/2 and retry 10/16; all other and unknown codes halt with an alert, except an explicitly requested stop before merge (20), which halts quietly without an alert or label. BLOCK and unknown review verdicts return 25. The proposed v3.5 policy also treats 28 as a quiet, cancellable spec-capacity wait (ADR 0007). Sources: `templates/scripts/bureau-config.sh:3578`; `templates/scripts/bureau-config.sh:3596`; `templates/scripts/shepherd.sh:893`.

## Rationale

The adapter centralizes provider differences while the stage and ownership protocols protect user checkouts and make retries inspectable. The reduced coupling to provider conversations is an inference from these boundaries; the separate workspace modes and exit semantics are explicit in the sources.

## Consequences

Each call pays authentication/startup overhead and produces its own diagnostics. Provider configuration can vary by stage without giving a model independent routing authority. Scripts and their shared helpers must be resynced coherently; changing an exit's meaning affects supervisors and alerts.

## Known limits

Ownership is local to the shared Git directory, not a distributed lock or atomic Linear transaction. App tasks use the app's selected model and do not create a new headless provider call automatically. A detached child session can outlive a provider process group; fixture coverage does not qualify unattended operation in every installation. ADR 0007 records the proposed addition of capacity-wait exit 28.

## Reversal condition

Reconsider if a durable executor can preserve equivalent stage identity, permissions, ownership, structured-result checks and cancellation semantics while sharing a provider session, with representative operational evidence.

## Evidence

- `tests/test_shared_runtime.sh` exercises ownership, protected checkouts, stale app completions and prepare/finish behavior through the shared-runtime fixtures.
- `tests/test_provider_adapter.sh` exercises provider argument/result contracts, authentication and normalized failures with fake CLIs.
- `tests/test_interrupted_run_resume.sh` exercises preservation and explicit recovery after interruption.

## Open points

The legacy helper comment at templates/scripts/bureau-config.sh:709 says anything other than Codex resolves to Claude, but its implementation at :724 rejects an unknown runner with 22, consistent with docs/provider-runtime.md:23. The stale comment remains unchanged. Registered-worker exceptions and proposed evidence/gate behavior are recorded separately in ADRs 0003, 0005 and 0008 rather than treated as v2.0.0 guarantees.
