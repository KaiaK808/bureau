# 0002 Explicit human merge authority and one automated merge gate

- **Status**: accepted (derived)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `8fdde36` (manual mode refuses without a Merge state and disables dispatch); `docs/release-notes-v3.0.0.md:15` (released merge policy); `docs/configuration.md:76` (mode and defaults); `docs/configuration.md:78` (CI/base gates); `docs/stage-protocol.md:86` (stop and app boundaries); `templates/scripts/merge-pipeline.sh:589` (final gate rerun and pinned merge); `templates/scripts/bureau-config.sh:2037` (actual PR-base check).

## Context

A review APPROVE is an input to a merge decision. An adopter may reserve merging and rebasing for a human, while another may authorize automation. Cached mergeability alone can overlook red CI or a base that advanced after review.

## Options considered

- Let every APPROVE merge immediately for low latency, accepting stale-base and CI races and removing the human boundary.
- Require humans to merge in every installation, preserving universal human control but removing the existing automated workflow.
- Configure manual or auto authority explicitly and route every automated merge through one gate, accepting extra checks and waiting states.

## Decision

Honor agents.merge_mode: manual disables merge and rebase before external work, cannot be overridden by forced dispatch, requires a Merge state and parks an approved ticket there for a human. Invalid modes fall closed to manual; absent/null remains the released auto default. Sources: `8fdde36`; `docs/configuration.md:76`.

In auto mode, route inline review merges and dedicated Merge workers through the same merge pipeline, independently check CI and the current PR base, rerun the gates immediately before merging and pin the merged head. App finish and an outside green test do not execute or authorize a merge. Sources: `templates/scripts/merge-pipeline.sh:589`; `docs/configuration.md:78`; `docs/stage-protocol.md:86`.

## Rationale

Authority must survive dispatch overrides and configuration mistakes. Checking independently of cached mergeability and repeating checks at publication addresses the observed race directly. Configurable authority reflects the released product; a universal manual default would be a new decision.

## Consequences

Manual installations need a configured Merge state and an operator to complete the human handoff. Automated workflows may wait or halt after approval. Merge-policy exceptions for repositories without CI require deliberate configuration and are not a debugging shortcut.

## Known limits

Manual GitHub operations are outside Bureau's automated gate. A default-auto configuration permits the inline merge path even when the dedicated merge agent is disabled. Local tests and reviews do not substitute for the gate's current remote checks, and no gate can prevent all external changes after its final observation.

## Reversal condition

Replace this decision only if the authority default or available modes change deliberately, or a different merge mechanism demonstrably preserves the human boundary and equivalent current-head/current-base checks.

## Evidence

- `tests/test_merge_mode.sh` exercises manual-mode refusal, forced dispatch and missing Merge-state behavior.
- `tests/test_merge_pipeline_correctness.sh` exercises red/pending CI, stale bases, just-in-time races and head-pinned merge calls.
- `tests/test_inline_merge_result.sh` checks that review acts on the shared merge pipeline's actual result.
- `tests/test_merge_gate_switches.sh` exercises the explicit CI/base configuration exceptions.

## Open points

docs/configuration.md:79 describes the up-to-date gate as comparison with origin/main. templates/scripts/bureau-config.sh:2037 instead resolves the PR's actual baseRefName and compares that branch's current commit; stacked PRs need the latter distinction. This wording disagreement remains unresolved here.
