# 0006 One Codex wire projection over canonical result schemas

- **Status**: proposed (v3.5, PR #81)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `6589bd1` (shared Codex schema projection); `1282b9d` (PR #82 composes insights and removes a duplicate projection); `templates/scripts/bureau-gates.py:19` (recursive wire projection); `templates/scripts/bureau-gates.py:38` (canonical normalization); `templates/scripts/bureau-provider.py:413` (receipt recovery normalization); `templates/scripts/bureau-provider.py:614` (forward normalization and validation); `docs/stage-protocol.md:106` (insight composition); `docs/provider-runtime.md:69` (QA compatibility).

## Context

Canonical Bureau results have optional fields for backwards compatibility. Codex's strict provider schema needs every object property to be required, so sending the canonical schema directly is insufficient. Gate blockers and advisory insights must compose without diverging schemas or changing canonical receipt identities.

## Options considered

- Make every canonical field mandatory, simplifying the provider contract but breaking legacy results and other providers.
- Maintain a separate schema conversion for each feature, reducing local coupling while allowing blocker, insight and recovery behavior to drift.
- Use one recursive Codex projection with inverse optional-null omission, preserving canonical compatibility at the cost of two explicit representations.

## Decision

Use bureau-gates.py codex_schema as the sole Codex wire projection: recursively require object properties and make originally optional properties nullable while preserving their original constraints. Keep the canonical schemas as the compatibility and receipt-hash contract. Sources: `templates/scripts/bureau-gates.py:19`; `6589bd1`; `1282b9d`.

On both normal forwarding and same-attempt receipt recovery, omit only declared optional null fields before advisory normalization, canonical validation and result hashing. Preserve required nulls, unknown keys and wrong values for validation; do not erase product/security/human blockers. Apply this same path to gate fields and insights. Sources: `templates/scripts/bureau-gates.py:38`; `templates/scripts/bureau-provider.py:413`; `templates/scripts/bureau-provider.py:614`; `docs/stage-protocol.md:106`.

## Rationale

One projection prevents optional-field features from independently rewriting the wire contract. Canonicalization before hashing makes forward and recovery identities agree even when the wire explicitly used null to mean omission.

## Consequences

Codex results must satisfy the projected strict shape, while old canonical results can remain valid. Claude's insight array requirement is a separate provider contract, not another Codex projection. Provider, schemas and recovery code need coherent resyncs when optional fields change.

## Known limits

The local validator implements the subset of schema features used by Bureau, not a general JSON Schema engine. Advisory insight normalization can drop malformed optional observations by design. A structurally valid blocker is not proof that its command ran; ADR 0005 supplies that separate check. Fixture tests do not establish live structured-output compatibility for every CLI/model.

## Reversal condition

Replace this projection if the supported Codex contract can consume canonical optional fields directly, or if a required schema feature cannot be represented without changing semantics. Preserve legacy compatibility and canonical receipt identity during that migration.

## Evidence

- `tests/test_provider_adapter.sh` exercises strict wire schemas, optional-null normalization, unknown/required invalid fields and identical forward/recovered digests, including gate-plus-insight composition.
- `tests/provider_adapter_test.py`, invoked by `tests/test_provider_adapter.sh`, exercises same-attempt canonical-result recovery and digest validation.

## Open points

No competing Codex projection remains at the snapshot: commit 1282b9d explicitly removed the PR #82 copy and routes that feature through the PR #81 function. Live provider qualification remains open; no schema change is inferred from fixture success.
