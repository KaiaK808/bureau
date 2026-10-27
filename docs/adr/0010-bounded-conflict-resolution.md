# 0010 Five fixed conflict rules and explicit escalation

- **Status**: accepted (derived)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `6763c26` (initial fixed conflict resolver, carried into released versions); `templates/scripts/bureau-config.sh:2468` (merge helper); `templates/scripts/bureau-config.sh:2483` (five path rules); `templates/scripts/implement-pipeline.sh:614` (implementation conflict hold); `templates/scripts/qa-pipeline.sh:90` (QA conflict route); `templates/scripts/code-review-pipeline.sh:201` (pinned-base conflict route); `templates/scripts/rebase-pipeline.sh:163` (rebase abort and human label).

## Context

Validation merges can repeatedly conflict in generated metadata and operational files. Mechanical handling of those paths reduces repeated human work, but applying model-generated conflict resolution to arbitrary source code would change the reviewed behavior. The current implementation has a small fixed rule set rather than a general conflict-solving agent.

## Options considered

- Refuse all conflicts, preserving maximum conservatism but repeatedly blocking known mechanical cases.
- Ask a model to resolve every conflict, improving autonomy while changing source semantics without a bounded deterministic rule.
- Resolve five known path classes mechanically and abort anything remaining, accepting explicit exceptions and stage-specific human recovery.

## Decision

For validation merges, keep ours for .specify/feature.json; strip merge markers to combine CLAUDE.md and .gitignore; take theirs for rust/Cargo.lock; remove tracked logs/queue-*.log. Complete the merge only when the helper found exclusively those classes and no unmerged paths remain; otherwise abort and request resolution. Sources: `6763c26`; `templates/scripts/bureau-config.sh:2483`.

Keep unresolved conflicts on explicit stage recovery paths: implement applies needs-human and exits 17; QA and code review return the ticket to Build with 17; the rebase stage aborts and applies needs-human. Do not infer a universal identical label/exit route from the shared helper. Sources: `templates/scripts/implement-pipeline.sh:614`; `templates/scripts/qa-pipeline.sh:90`; `templates/scripts/code-review-pipeline.sh:201`; `templates/scripts/rebase-pipeline.sh:163`.

## Rationale

The helper's comments identify repetitive legacy metadata conflicts as the reason for the exceptions. Keeping arbitrary source paths outside the resolver avoids silently selecting program semantics. Stage-specific recovery reflects the actual callers rather than imposing a new uniform policy.

## Consequences

These five classes are fixed implementation policy, not a configurable semantic merge engine. Combining instructions can retain inconsistent prose, and taking a lockfile from the base relies on later project validation. Unknown source conflicts still need a human; conflict comments are throttled independently of the abort.

## Known limits

The fifth rule is a path pattern, not one literal file, and two classes share the marker-stripping operation. A needs-human label is not currently applied by every validation-merge caller. The pinned-base regression directly exercises the CLAUDE.md case; it does not establish complete independent coverage of all five classes. No new resolver coverage is introduced by this documentation-only derivation.

## Reversal condition

Revisit on evidence that one mechanical rule loses meaningful changes, when another path needs deterministic handling, or when stage recovery should be unified. Each broader exception needs its own justified semantics and regression evidence before implementation.

## Evidence

- `tests/test_review_base.sh` exercises the real pinned-base helper resolving a CLAUDE.md conflict while preserving both sides and removing markers.
- `tests/test_merge_pipeline_correctness.sh` checks that a conflicted remote merge gate is blocked rather than automatically merged.
- `tests/test_implement_merge_worker.sh` rejects a committed conflict resolution from the automatic clean-merge reuse path; this protects reuse and is not coverage of every resolver branch.

## Open points

The code does not implement a single otherwise-needs-human route across all stages: QA/review route to Build, while implement and rebase label the hold. Whether to unify those routes remains a new design choice. A dedicated matrix exercising every fixed path and an unknown-path abort was not identified in the inspected tests; this coverage gap remains open.
