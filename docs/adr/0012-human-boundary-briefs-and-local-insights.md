# 0012 Metadata briefs at human boundaries and local advisory insights

- **Status**: proposed (v3.5, PR #82)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `1282b9d` (acceptance briefs and advisory insights); `CHANGELOG.md:28` (human handoff package); `docs/stage-protocol.md:96` (brief projection); `docs/stage-protocol.md:100` (delivery ledger); `docs/stage-protocol.md:106` (optional observation shape); `docs/stage-protocol.md:110` (local-only projection); `docs/configuration.md:320` (retention and no new configuration); `templates/scripts/bureau-acceptance.py:81` (deterministic renderer); `templates/scripts/bureau-evidence.py:84` (advisory normalization).

## Context

A human merge boundary needs a compact statement of intent, change identity, tests, review and missing evidence. Raw provider prose is unsuitable as an acceptance record. Stages may also observe prompt/tool friction worth later triage, but such observations must not silently change rules or be copied into public comments.

## Options considered

- Leave the human to reconstruct evidence from logs, minimizing publication code while increasing handoff effort and ambiguity.
- Publish provider summaries and automatically apply suggested lessons, making feedback immediate but exposing private prose and turning observations into authority.
- Publish a deterministic metadata brief and keep bounded advisory observations local for human triage, adding delivery/retention machinery without changing merge authorization.

## Decision

At manual review APPROVE, requested review stops and shepherd stops already at Merge, render one metadata brief per PR/HEAD with explicit missing evidence and store-relative identifiers. Persist its body/digest and independent ticket/PR delivery status before publishing; retry only missing destinations. Publication preserves the original stage outcome and declares no acceptance, freshness or merge. Sources: `1282b9d`; `docs/stage-protocol.md:96`; `docs/stage-protocol.md:100`; `templates/scripts/bureau-acceptance.py:81`.

Treat insights as the explicit local free-text exception to ADR 0003: normally empty, up to three single-line observations of at most 280 characters per attempt, retained privately for at most 2,048 rows/30 days under the existing evidence budget. Drop malformed or unsafe observations best effort without failing the stage, remove the field recursively from external comment projections, and draft learning candidates only for human triage. Sources: `docs/stage-protocol.md:106`; `docs/stage-protocol.md:110`; `docs/configuration.md:320`; `templates/scripts/bureau-evidence.py:84`.

## Rationale

Deterministic identity and explicit missing fields make a handoff inspectable without claiming approval that never occurred. Separating local advisory text from published metadata preserves a useful learning channel while keeping it outside authorization and automatic rule changes.

## Consequences

Delivery to two services requires a local ledger and publication lock; unavailable storage skips publication rather than changing the stop. Validation-merge review evidence needs pinned input/base/parent correlation. Insight collection shares retention/capacity with evidence, so observations and deduplication can expire. Resync scripts and interfaces together; no new configuration key or historical backfill is provided.

## Known limits

The content filter is best effort and may miss private material or reject harmless prose; authors still must not supply secrets or private text. Removing the insights field does not detect someone copying its content into another prose field. Local ledgers and remote markers cannot guarantee exactly-once delivery across hosts, external deletions or service visibility delays. Frozen briefs can become stale or lose referenced evidence through retention.

## Reversal condition

Revisit if handoff metadata cannot support the human decision, delivery ambiguity becomes unacceptable, or advisory text creates an observed disclosure path. Any broader publication or automatic learning requires an explicit authority/privacy decision rather than expanding this exception implicitly.

## Evidence

- `tests/test_acceptance_insights.sh` exercises brief projection, missing evidence, independent deliveries, linked-worktree storage and bounded local observations.
- `tests/test_acceptance_insights_negative_controls.sh` mutation-checks privacy, delivery identity and observation rules.
- `tests/test_insight_comment_privacy.sh` exercises removal of insights from actual stage comment paths and failed-projection fallbacks.
- `tests/test_comment_projection.sh` checks recursive external projection independently of advisory content filtering.

## Open points

No core brief/insight contract disagreement was found at this snapshot. Whether the two known nonblocking advisory-filter gaps noted in commit 1282b9d warrant stricter filtering remains a triage question; this ADR retains the documented best-effort limit and does not claim complete secret detection or live rollout acceptance.
