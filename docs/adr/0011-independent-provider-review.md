# 0011 Independent review with optional Codex review of Claude builds

- **Status**: accepted (derived)
- **Date**: 2026-10-29
- **Decided by**: Kai Ebert
- **Sources**: `7ebaf8a` (per-stage providers, released in v2.0.0); `docs/recipes.md:174` (Codex review with Claude default); `docs/provider-runtime.md:23` (runner precedence); `docs/provider-runtime.md:29` (review sandbox default); `templates/scripts/bureau-provider.py:67` (resolved review sandbox default); `templates/scripts/bureau-provider.py:458` (Codex sandbox argument); `templates/scripts/bureau-provider.py:478` (Claude permission flags); `templates/scripts/code-review-pipeline.sh:365` (independent specialist invocations); `templates/scripts/code-review-pipeline.sh:589` (deterministic verdict floors); `docs/release-notes-v3.0.0.md:17` (released verdict ordering); `templates/scripts/bureau-adr.py:100` (unreleased opt-in base/head authority check); `templates/scripts/code-review-pipeline.sh:318` (context and schema selection); `docs/stage-protocol.md:138` (review contract); `templates/scripts/bureau-review.py:214` (structured merge, anchors and shadow requests); `templates/scripts/bureau-specialist.schema.json:1` (specialist contract); `templates/scripts/bureau-finding-verifier.schema.json:1` (checker contract); `templates/scripts/bureau-evidence.py:122` (metadata projection).

## Context

Implementation's own completion report is insufficient review evidence. Separate review calls can inspect pinned changes from correctness, security and performance perspectives, and a different provider can reduce dependence on the implementer's approach. Bureau supports this mixed route without requiring provider diversity in every installation.

## Options considered

- Reuse the implementation conversation as its own reviewer, avoiding startup cost but preserving its assumptions and context.
- Force a second provider for all reviews, creating diversity but requiring both accounts and exceeding the released configuration contract.
- Run a separate review stage with independent provider selection and deterministic verdict floors, enabling Codex review of Claude work while retaining deployment choice.

## Decision

Allow code_review to select Codex independently while the default implementation provider stays Claude. Resolve that stage's runner through the shared provider adapter; use separate correctness, security and performance calls plus a structured merge over pinned PR inputs. This is an available review configuration, not a mandatory cross-provider default. Sources: `7ebaf8a`; `docs/recipes.md:174`; `docs/provider-runtime.md:23`; `templates/scripts/code-review-pipeline.sh:365`.

Codex code review defaults to read-only; the Claude adapter ignores the sandbox setting and uses --dangerously-skip-permissions. Let the shell apply the ordered security/build/cycle verdict rules and publish routing. A red build cannot soften BLOCK, and approval remains subject to ADR 0002's merge authority. Sources: `docs/provider-runtime.md:29`; `templates/scripts/bureau-provider.py:67`; `templates/scripts/bureau-provider.py:458`; `templates/scripts/bureau-provider.py:478`; `templates/scripts/code-review-pipeline.sh:589`; `docs/release-notes-v3.0.0.md:17`.


Re-derived on 2026-10-29 for unreleased v3.6: merge complete validated specialist findings in code, taking highest severity for duplicate path/range/normalized-title claims and retaining distinct details and ADR references. Render a fixed findings template under unchanged protocol headers. Malformed/missing specialist JSON uses the existing model merger and records fallback roles as metadata; readable critical/security/source floors survive. Source anchors bind head/base paths, regular blob IDs, inclusive lines or explicit whole-file scope and exact-byte digests. Grounding defaults off; required grounding adds BLOCK/25, while parse errors stay 22. Opt-in verification is bounded shadow observation only: a fresh checker sees claim, pinned source, contract and anchors, returns finding/anchor identities and an enum outcome, and cannot lift any floor. Merge/grounding/verifier policy invalidates prior approvals. Sources: `templates/scripts/bureau-review.py:214`; `templates/scripts/bureau-specialist.schema.json`; `templates/scripts/bureau-finding-verifier.schema.json`; `templates/scripts/code-review-pipeline.sh`; `templates/scripts/bureau-evidence.py`.

## Rationale

Independent invocations separate review from the implementation conversation, while per-stage selection allows an adversarial model/provider choice without duplicating stage machinery. Provider diversity may reduce correlated mistakes; this benefit is an inference, not a tested guarantee of correctness.

## Consequences

Mixed-provider review requires the selected provider's executable, login and model access, and incurs several calls. All specialists, fallback merger and shadow checker use the review stage's selected runner; they do not automatically use different providers from one another. The deterministic security and build floors can override the model's optimistic verdict.

Follow-up on 2026-10-28 for the unreleased v3.6 change: review can opt into ADR grounding while retaining independent calls and verdict floors. `agents.review_adr_context` defaults to false. The bounded index compares pinned PR base and head; only accepted ADRs with identical paths and blob IDs on both inputs are settled, including for known-limit exclusions. Added, modified and removed ADRs are non-authoritative proposals under review, even with accepted status. Findings may cite included settled or proposal IDs through optional `adr_refs`; citing a proposal never waives or downgrades a finding. Contradicting a settled decision requires new evidence justifying reopening it. See `templates/scripts/bureau-adr.py:100`, `templates/scripts/code-review-pipeline.sh:318` and `docs/stage-protocol.md:138` for this additional input contract. The external review brief uses the same rule.

## Known limits

Different calls or providers do not prove findings are true or independent. The optional fresh verifier is shadow-only, capped at eight findings, and no enforced provider difference from implementation. A read-only model sandbox does not prevent the deterministic executor from running its own validation build. Test fixtures check selection, scope and routing rather than review quality on arbitrary code.

ADR context is a bounded index and prompt instruction, not proof that a finding respects a decision. Reviewers must read full pinned scope and Known limits when needed. Only included IDs are valid runtime citations; proposal IDs carry no settled authority, and oversized or budget-omitted records cannot be cited. Enabled review selects `bureau-review-adr.schema.json`. Disabled review retains the exact legacy `bureau-review.schema.json` bytes, Codex wire projection, validation and receipt schema digest, as well as unchanged fallback prompts and comments; unsolicited `adr_refs` remain unexpected result fields.

## Reversal condition

Revisit if measured correlated errors justify mandatory diversity or a separate finding-verification pass, or if specialist fan-out costs exceed its observed value. Such enforcement would be a new runtime decision rather than a stronger interpretation of the existing recipe.

## Evidence

- `tests/test_review_merge.sh` checks full three-specialist merge, duplicates/severity/ADR preservation, floors, fixed-template bytes, worker headers/round counts, malformed fallback metadata, pinned/deleted-base anchors, off/required grounding, parse exit 22 and shadow refutation without verdict changes using fake providers.
- `tests/test_review_merge_negative_control.sh` drops performance findings and bypasses anchor validation in private copies; unchanged positive assertions must fail.

- `tests/test_provider_adapter.sh` checks independent stage runner/model resolution and provider result contracts.
- `tests/test_review_base.sh` checks all three specialists' pinned diff scope and invalidation when the PR head/base changes.
- `tests/test_review_verdict_order.sh` checks the security/build/cycle ordering after specialist output.
- `tests/test_review_build_floor.sh` checks that a red build preserves BLOCK.
- `tests/test_review_adr.sh` checks bounded base/head indexing, unchanged accepted authority, non-authoritative added/modified/removed proposals, pinned reads, optional reference validation, metadata-only receipt references and disabled prompt/comment/wire/schema-digest parity against fixed legacy fixtures using fake providers.
- `tests/test_review_adr_negative_control.sh` checks that removing ADR prompt insertion or bypassing reference validation in temporary copies of the current scripts fails unchanged positive assertions; no release tag or history is required.

## Open points

Whether to require cross-provider diversity or enforce verifier disagreements remains open. Enforced mode is deferred; model refutation cannot lift critical/security/BLOCK floors. The shipped mixed-provider recipe supports the route; it does not enforce either policy, and this ADR does not turn that operational choice into a guarantee.

The unqualified review sandbox wording in docs/provider-runtime.md:29 remains ambiguous: templates/scripts/bureau-provider.py:458 applies the setting only to Codex, while :478 invokes Claude with --dangerously-skip-permissions. The broader documentation has not been reconciled with that provider-specific behavior.
