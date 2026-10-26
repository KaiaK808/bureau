# 0005 Machine-proven outside gates with a single publication hop

- **Status**: proposed (v3.5, PR #81)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `6589bd1` (machine-evidence gate recovery); `82542e4` (executor publication binding); `docs/release-notes-v3.3.0.md:54` (released prefix-based limit); `docs/stage-protocol.md:74` (eligibility and publication identity); `docs/stage-protocol.md:76` (reservation and execution); `docs/provider-runtime.md:61` (outcomes and unchanged authority); `templates/scripts/bureau-evidence.py:265` (live publication verification).

## Context

A Codex sandbox can deny a configured test gate that the deterministic executor can run successfully. The released v3.3 prefix-based gate trusted the provider's diagnosis of the cause. The executor also commits stage edits before reconciliation, moving HEAD beyond the receipt's input even when that move is legitimate publication.

## Options considered

- Accept the provider's sandbox diagnosis alone, preserving convenient recovery but allowing wording to stand in for execution proof.
- Refuse every sandbox-related hold or HEAD change, minimizing inference but blocking legitimate completed work after executor publication.
- Require failed-command evidence and exact attempt identity, with one verified executor publication hop and one reserved outside run, at the cost of strict event matching.

## Decision

Recover only a declared configured-gate hold backed by a successful same-attempt receipt, canonical result and command digests, and a failed or aborted Codex execution at the repository root. Typed blockers take precedence; legacy SANDBOX_GATE wording grants no exemption from machine proof. Sources: `6589bd1`; `docs/stage-protocol.md:74`.

Accept receipt HEAD A only at A, or at exactly one executor publication commit B whose same-attempt record and live Git prove sole parent A and the Bureau-Generated trailer. Reserve and rerun at B after publication; chains, siblings, merges, foreign attempts and missing records remain ineligible. Sources: `82542e4`; `templates/scripts/bureau-evidence.py:265`; `docs/stage-protocol.md:74`.

Atomically reserve at most one outside execution per stage attempt, gate and HEAD, revalidate immediately before running and use the existing filtered environment, system Bash, timeout and cancellation rules. Green clears only qualifying gate holds; other blockers survive, cancellation remains 130, and merge authority is unchanged. QA reuses its final outside phase rather than adding another run. Sources: `docs/stage-protocol.md:76`; `docs/provider-runtime.md:61`.

## Rationale

Execution identity and current-head checks replace an unverified explanation with a reproducible gate outcome. The narrow publication exception recognizes the executor's own commit without treating arbitrary intervening history as equivalent evidence.

## Consequences

Missing, ambiguous or reordered CLI/rollout events disable recovery. Incomplete reservations consume the one allowed execution. Evidence retains only identities, measurements, pointers and digests; original outside output lives in normal stage logs. Resync provider, evidence, gates, config, implementation/QA and schemas together, then restart drivers.

## Known limits

A failed execution plus outside green proves those outcomes, not the environmental cause or absence of undeclared product issues. The local store is not tamper-proof against a malicious repository or CLI. Live event compatibility and representative installation acceptance remain pending. Local green does not replace CI, current-base checks or human merge authority.

## Reversal condition

Revisit if supported CLI events cannot establish the required identity reliably, or if publication needs more than one commit. Broaden accepted history only with an explicit new proof model and negative controls against stale or foreign attempts.

## Evidence

- `tests/test_provider_adapter.sh` exercises command/event matching, typed blockers, receipt identity and one-run reservations.
- `tests/test_gate_publication.sh` exercises the same-attempt, single-parent publication allowance and rejection of other HEAD changes.
- `tests/test_codex_stage_publication.sh` exercises executor publication in actual implementation/QA stage paths.
- `tests/test_codex_sandbox_gate.sh` and `tests/test_codex_qa_sandbox_gate.sh` exercise stage routing, outside outcomes and independent holds.

## Open points

templates/scripts/implement-pipeline.sh:706 still tells the provider to begin sandbox reasons with the legacy prefix, while the appended instruction at :707 says the typed reason has no required prefix. docs/provider-runtime.md:57 and :61 describe both forms under the same machine-proof rule. The contradictory prompt wording is left intact; this ADR derives authorization from the reconciler, not either sentence alone.
