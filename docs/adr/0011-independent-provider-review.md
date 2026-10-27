# 0011 Independent review with optional Codex review of Claude builds

- **Status**: accepted (derived)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `7ebaf8a` (per-stage providers, released in v2.0.0); `docs/recipes.md:174` (Codex review with Claude default); `docs/provider-runtime.md:23` (runner precedence); `docs/provider-runtime.md:29` (review sandbox default); `templates/scripts/bureau-provider.py:67` (resolved review sandbox default); `templates/scripts/bureau-provider.py:458` (Codex sandbox argument); `templates/scripts/bureau-provider.py:478` (Claude permission flags); `templates/scripts/code-review-pipeline.sh:365` (independent specialist invocations); `templates/scripts/code-review-pipeline.sh:589` (deterministic verdict floors); `docs/release-notes-v3.0.0.md:17` (released verdict ordering).

## Context

Implementation's own completion report is insufficient review evidence. Separate review calls can inspect pinned changes from correctness, security and performance perspectives, and a different provider can reduce dependence on the implementer's approach. Bureau supports this mixed route without requiring provider diversity in every installation.

## Options considered

- Reuse the implementation conversation as its own reviewer, avoiding startup cost but preserving its assumptions and context.
- Force a second provider for all reviews, creating diversity but requiring both accounts and exceeding the released configuration contract.
- Run a separate review stage with independent provider selection and deterministic verdict floors, enabling Codex review of Claude work while retaining deployment choice.

## Decision

Allow code_review to select Codex independently while the default implementation provider stays Claude. Resolve that stage's runner through the shared provider adapter; use separate correctness, security and performance calls plus a merged verdict over pinned PR inputs. This is an available review configuration, not a mandatory cross-provider default. Sources: `7ebaf8a`; `docs/recipes.md:174`; `docs/provider-runtime.md:23`; `templates/scripts/code-review-pipeline.sh:365`.

Codex code review defaults to read-only; the Claude adapter ignores the sandbox setting and uses --dangerously-skip-permissions. Let the shell apply the ordered security/build/cycle verdict rules and publish routing. A red build cannot soften BLOCK, and approval remains subject to ADR 0002's merge authority. Sources: `docs/provider-runtime.md:29`; `templates/scripts/bureau-provider.py:67`; `templates/scripts/bureau-provider.py:458`; `templates/scripts/bureau-provider.py:478`; `templates/scripts/code-review-pipeline.sh:589`; `docs/release-notes-v3.0.0.md:17`.

## Rationale

Independent invocations separate review from the implementation conversation, while per-stage selection allows an adversarial model/provider choice without duplicating stage machinery. Provider diversity may reduce correlated mistakes; this benefit is an inference, not a tested guarantee of correctness.

## Consequences

Mixed-provider review requires the selected provider's executable, login and model access, and incurs several calls. All specialists and the verdict pass use the review stage's selected runner; they do not automatically use different providers from one another. The deterministic security and build floors can override the model's optimistic verdict.

## Known limits

Different calls or providers do not prove findings are true or independent. There is no mandatory fresh verifier per finding, and no enforced provider difference from implementation. A read-only model sandbox does not prevent the deterministic executor from running its own validation build. Test fixtures check selection, scope and routing rather than review quality on arbitrary code.

## Reversal condition

Revisit if measured correlated errors justify mandatory diversity or a separate finding-verification pass, or if specialist fan-out costs exceed its observed value. Such enforcement would be a new runtime decision rather than a stronger interpretation of the existing recipe.

## Evidence

- `tests/test_provider_adapter.sh` checks independent stage runner/model resolution and provider result contracts.
- `tests/test_review_base.sh` checks all three specialists' pinned diff scope and invalidation when the PR head/base changes.
- `tests/test_review_verdict_order.sh` checks the security/build/cycle ordering after specialist output.
- `tests/test_review_build_floor.sh` checks that a red build preserves BLOCK.

## Open points

Whether to require cross-provider diversity or a fresh verification pass for each finding remains open. The shipped mixed-provider recipe supports the route; it does not enforce either policy, and this ADR does not turn that operational choice into a guarantee.

The unqualified review sandbox wording in docs/provider-runtime.md:29 remains ambiguous: templates/scripts/bureau-provider.py:458 applies the setting only to Codex, while :478 invokes Claude with --dangerously-skip-permissions. The broader documentation has not been reconciled with that provider-specific behavior.
