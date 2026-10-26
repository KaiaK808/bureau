# NNNN Decision title

- **Status**: accepted (derived) | proposed (v3.5, PR #n) | superseded by NNNN
- **Date**: YYYY-MM-DD
- **Decided by**: Kai Ebert
- **Sources**: `path/to/source:line` (describe the observed rule); `commit-hash` (describe its introduction or revision).

## Context

Describe the question, operating constraints and current behavior. Date the derivation and distinguish released behavior from the unmerged stack. Use generic incident descriptions without private names or ticket quotations.

## Options considered

- Describe one reconstructed alternative with its benefit and cost.
- Describe another reconstructed alternative with its benefit and cost. Do not invent evidence of a historical debate.

## Decision

State the observed choice and its precise scope. Every paragraph or list item here must cite a commit, release or repository path with a line number, for example `path/to/source:line`; include that reference in Sources too.

## Rationale

Explain why the observed choice fits the constraints. Identify inferred reasoning when the source does not state the reason explicitly.

## Consequences

Describe operational benefits, costs and any required coherent resync or compatibility action.

## Known limits

Describe the actual boundary, qualification limits and what the implementation cannot guarantee.

## Reversal condition

State what evidence or changed requirement would justify a replacement decision.

## Evidence

- `tests/test_adrs.sh` checks this document contract. Replace or supplement this entry in a real ADR with existing tests or CI checks that exercise its architectural rule, describing their actual coverage.

## Open points

Record unresolved code/documentation disagreements with both locations. Do not repair them in a derived ADR. Say explicitly when no disagreement was found within the inspected scope.
