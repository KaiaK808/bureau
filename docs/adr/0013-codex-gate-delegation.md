# 0013 Optional Codex gate delegation to Bureau

- **Status**: accepted (derived)
- **Date**: 2026-11-02
- **Decided by**: Kai Ebert
- **Sources**: `templates/scripts/bureau-provider.py:99` (opt-in configuration); `templates/scripts/bureau-provider.py:521` (agent brief); `templates/scripts/bureau-provider.py:975` (outside environment and teardown); `templates/scripts/bureau-provider.py:1065` (receipt-bound check and shared execution); `templates/scripts/bureau-evidence.py:905` (per-pass reservation); `templates/scripts/implement-pipeline.sh:1292` (pass evidence and feedback); `templates/scripts/implement-pipeline.sh:1587` (final receipt reuse); `templates/scripts/qa-pipeline.sh:294` (final QA check and routing).

## Context

The existing sandbox-only rerun repairs a configured gate denied inside Codex, but the initial agent run can still fail on network/permissions, leave fixtures it cannot stop, or ask for an escape. Bureau may already run inside an isolated outer runner. This record derives the opt-in behavior in the current working tree; it is not a release or a live-runner qualification.

## Options considered

- Keep recovery only: no configuration change, but every affected agent still encounters the inner boundary first.
- Disable the agent sandbox: broader editing authority than this problem needs.
- Delegate the configured gate to Bureau while retaining Codex's editing sandbox: explicit operator choice using the existing runner and evidence.

## Decision

Default the boolean agents.providers.codex.gate_outside_sandbox to false. When enabled for implementation/QA, the brief says Bureau owns the full configured gate, permits focused tests and forbids asking for a sandbox escape merely to run the suite. The agent reports edits and independent blockers truthfully. Sources: `templates/scripts/bureau-provider.py:99`; `templates/scripts/bureau-provider.py:521`.

Bureau checks after each successful Codex implementation pass, after executor publication, and in QA's final phase after its pass/publication. Match the existing completion receipt, canonical result digest and current HEAD, including the existing single publication hop. Reserve the configured command once per provider pass/HEAD using the same reservation and outside execution path; final implementation validation reuses it. Keep sandbox-only recovery intact, including its failed-execution requirement; delegation normally makes it unnecessary. Sources: `templates/scripts/bureau-provider.py:1065`; `templates/scripts/bureau-evidence.py:905`; `templates/scripts/implement-pipeline.sh:1587`; `templates/scripts/qa-pipeline.sh:294`.

The gate receives the stage worktree and outer runner/account's filesystem/network access, as today's outside rerun does, with the existing default or clean untrusted child-environment filter. Never pass Bureau service secrets, their carriers or startup hooks. Default retains other inherited test/tool credentials; clean keeps only the test environment allowlist, without agent-only login/network additions. The existing bounded outside teardown runs beyond the inner sandbox and stops the gate's remaining process-group writers before success evidence. Sources: `templates/scripts/bureau-provider.py:975`.

Record the outside execution receipt/measurements in existing stage evidence and leave output in normal outside-gate logs. Include the previous implementation result and log tail in the next brief; QA already receives initial shell output and retains the final log. Red partial passes inform the next pass; completed implementation red keeps exit 14, QA red keeps Build routing, and green never clears independent blockers. Sources: `templates/scripts/implement-pipeline.sh:1292`; `templates/scripts/implement-pipeline.sh:1587`; `templates/scripts/qa-pipeline.sh:294`.

## Rationale

Inference: moving the full gate to the supervisor removes the nested execution boundary without expanding editing authority. Reusing receipts, reservations and teardown keeps this a delegation choice rather than a second gate executor.

## Consequences

Operators must compare installation drift, resync the complete scripts scope and restart drivers before enabling. No evidence schema or exit vocabulary changes. Missing/mismatched evidence cannot yield green; false/absent leaves the existing briefs and execution flow intact.

## Known limits

Filtering is not filesystem isolation: same-account readable files remain readable. Network access remains bounded by the outer runner; this switch does not grant host access beyond it. Provider failures/timeouts do not authorize a delegated check. QA keeps its initial shell run/retry and skips the agent when its existing rules permit. Successful local tests with fake providers do not qualify live CLIs or adopting runners. Outside green is not merge authority.

## Reversal condition

Revisit if receipt identity cannot reliably bind the pass or installations demonstrate that the existing outside runner cannot enforce the intended environment boundary.

## Evidence

- `tests/test_provider_adapter.sh` checks default-off prompt/options, strict boolean configuration, filtered outside execution, per-pass receipt reuse and red evidence.
- `tests/test_codex_sandbox_gate.sh` checks implementation routing, repeated passes and outside-log feedback, alongside existing recovery controls.
- `tests/test_codex_qa_sandbox_gate.sh` checks QA delegation, red routing and preservation of independent blockers alongside existing recovery controls.

## Open points

Out of scope: process-group termination, signal traps, holds, leases, detached-process discovery, provider permissions, outer runner isolation, new credentials/network policy, CI and merge gates. No new disagreement was found within the inspected delegation flow; live installation qualification remains pending.
