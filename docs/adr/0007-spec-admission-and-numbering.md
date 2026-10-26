# 0007 Explicit spec capacity waits and shared number reservations

- **Status**: proposed (v3.5, PR #77)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `e7b9dd7` (admission and number allocation); `e7163a3` (reserved-branch retry, pruning and bounded lock); `53f49a6` (filtered Spec Kit helper calls); `CHANGELOG.md:88` (admission semantics); `CHANGELOG.md:89` (allocator scope and compatibility); `templates/scripts/bureau-config.sh:2849` (configured spec-worker reset base); `docs/configuration.md:74` (cap and Merge policy); `docs/configuration.md:179` (lock acquisition bound).

## Context

An adopter's night series exposed duplicate spec numbers when earlier specifications existed only on unmerged branches. Separately, a ticket waiting for a human merge consumed the manual-mode cap and prevented the next specification; reporting ordinary queue-empty made supervisors misclassify repeated capacity waits as stuck work.

## Options considered

- Scan only the current checkout and report a full cap as queue-empty, keeping allocation cheap but missing unpublished branches and conflating wait causes.
- Serialize all work through manual merge, avoiding overlap but reducing throughput and retaining the need to coordinate number allocation.
- Reserve numbers across the local shared repository and classify capacity separately, accepting additional fetch/scan/lock work and mode-dependent admission.

## Decision

Count Build Review but exclude Merge by default in manual mode; include Merge by default in auto mode and allow an explicit max_concurrent_include_merge boolean to override. A full cap exits 28, spec-capacity-wait, names holding identifiers/states and waits without stuck escalation or provider work. Sources: `e7b9dd7`; `CHANGELOG.md:88`; `docs/configuration.md:74`.

Before specify, fetch/prune all origin heads and reserve a per-issue spec number under the shared Git-directory lock using origin branches, local branches, worktrees and persistent reservations. Reuse the reservation on retry and attach its branch before writing artifacts; invoke the pinned Spec Kit 1.2.2 helper through the environment filter with the reserved number and reject a changed result. Bound lock acquisition to 300 seconds by default; timeout remains 124. Sources: `CHANGELOG.md:89`; `e7163a3`; `53f49a6`; `docs/configuration.md:179`.

## Rationale

Allocation must observe where earlier specifications actually live. A separate wait class preserves capacity as a scheduling condition rather than a defect. Excluding a human-owned Merge queue in manual mode improves throughput without excluding Build Review work.

## Consequences

Reservations persist and gaps are retained; deleting the reservation file can discard allocations not yet visible in Git. Manual-mode cap-one installations that require the previous single-flight behavior must set max_concurrent_include_merge to true. Stop all dispatchers before resyncing and restart them together: old and new consumers disagree about exits 2 and 28. The Spec Kit pin stays unchanged.

## Known limits

The cap is a Linear snapshot, not an atomic admission lock. Number uniqueness covers worktrees sharing this Git directory and fetched history, not separate clones/hosts or unreachable work. The acquisition timeout does not bound the Git scan after locking. Allocator base validation does not redefine every stage's reset/base policy.

## Reversal condition

Reconsider when number allocation must be unique across clones/hosts or the admission snapshot causes unacceptable oversubscription. That requires shared coordination beyond this local lock, not a wider claim about the current implementation.

## Evidence

- `tests/test_in_flight_count.sh` checks mode-dependent admission and explicit overrides.
- `tests/test_spec_number_alloc.sh` exercises real-Git scanning, concurrent allocation, pruning and lock controls.
- `tests/test_spec_feature_json.sh` checks reserved-number use with the pinned feature helper.
- `tests/test_spec_worker_retry.sh` exercises reserved-branch recovery after worker resets.
- `tests/test_spec_capacity_consumers.sh` and `tests/test_shepherd.sh` check that exit 28 remains a wait rather than escalation.
- `tests/test_spec_helper_env.sh` checks filtering at both helper call sites.

## Open points

ADR 0004 records the later checkpoint implementation's divergence exit 25 versus CHANGELOG.md:89's earlier exit 17. Keep that unresolved difference visible when using this allocator's retry documentation; do not copy the older exit into a new consumer.

CHANGELOG.md:89 also says disposable spec workers reset to origin/main, while templates/scripts/bureau-config.sh:2849 selects the origin ref from configured repo.base_branch (default main) unless a committed checkpoint overrides it. That reset-policy discrepancy remains open.
