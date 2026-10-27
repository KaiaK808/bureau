# 0004 Committed spec checkpoints and an explicit governance boundary

- **Status**: proposed (v3.5, PR #79)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `f386a85` (committed phase resume and governance); `a930eea` (ambiguous placeholder verbs left to review); `docs/stage-protocol.md:36` (planning restrictions); `docs/stage-protocol.md:46` (authoring placement); `docs/stage-protocol.md:50` (permanent cutoffs); `docs/stage-protocol.md:54` (validator limits); `docs/stage-protocol.md:58` (phase commits); `docs/stage-protocol.md:64` (resume invalidation); `templates/scripts/bureau-spec.py:78` (shared validator).

## Context

A spec timeout in plan should not require regenerating a completed specification, but unfinished edits cannot safely prove that a phase completed. Specs also need an explicit applicability decision about governance and security. A structural validator can catch misplaced sections and template leftovers without judging the adequacy of an explanation.

## Options considered

- Restart every phase after failure, keeping recovery simple but repeating completed work and provider cost.
- Resume from surviving files or unfinished worktrees, retaining progress but conflating partial edits with completed phases.
- Resume only a verified committed phase prefix and check governance structurally, retaining completed work while leaving substantive review to a reviewer.

## Decision

Commit each successful specify, plan and tasks phase locally and record the input fingerprint, commit and complete artifact hashes. A registered spec worker resets/cleans to a reachable, verified contiguous checkpoint prefix; changed inputs or failed spec review invalidate from specify. Preserve the existing single final push. Sources: `f386a85`; `docs/stage-protocol.md:58`; `docs/stage-protocol.md:64`.

Require an explicit Governance and security decision with an applicability rationale in plain column-0 text before the first code or HTML block; leading YAML front matter is allowed. The first fence-like, indented-code or HTML cutoff is permanent, including an earlier closed block. Apply the shared check in background spec review and app spec/spec_review completion. Sources: `docs/stage-protocol.md:46`; `docs/stage-protocol.md:50`; `templates/scripts/bureau-spec.py:78`.

Keep specify/plan free of measurements, probes, benchmarks and full test-gate runs. Treat placeholder detection as protection against common honest mistakes: ambiguous ordinary verbs and unrecognized wording remain the spec reviewer's responsibility. Sources: `docs/stage-protocol.md:36`; `a930eea`; `docs/stage-protocol.md:54`.

## Rationale

Git commits and artifact fingerprints supply a stronger completion boundary than file presence. Conservative cutoffs prevent quoted examples from supplying governance, while the documented placeholder limit avoids rejecting ordinary prose to approximate a semantic security check.

## Consequences

Local phase commits survive worker resets; unfinished edits do not. Separate clones need the actual commits, not just checkpoint metadata. Existing specs may need the governance section moved or added. Resync scripts and managed interfaces together; preserve customized Spec Kit templates, helpers and the constitution. Checkpoint I/O failure disables reuse for a fresh authorized run rather than changing review routing.

## Known limits

The validator is not a security boundary against deliberate obfuscation and cannot prove a rationale is meaningful. It deliberately does not recognize every placeholder or Markdown container. App tasks do not acquire automatic background phase checkpoints. Pre-version-2 checkpoint records are ignored, and unpublished history unavailable on another host cannot be recovered there.

## Reversal condition

Reconsider if committed phase reuse demonstrably produces stale specs despite input/artifact validation, or if the governance check must become a semantic or adversarial security boundary. Either change needs a different validation contract and compatibility analysis.

## Evidence

- `tests/test_spec_resume.sh` exercises real worker resets, committed checkpoint fingerprints and the governance cases.
- `tests/test_spec_resume_negative_controls.sh` mutation-checks checkpoint identity, invalidation and governance enforcement.
- `tests/governance_cases.py` supplies positive prose controls and rejection cases for cutoffs, placeholders and normalization.
- `tests/test_shared_runtime.sh` exercises the app completion check, including invalid governance after model PASS.

## Open points

The earlier admission changelog in CHANGELOG.md:89 says divergent reserved history stops with 17. docs/stage-protocol.md:64 and the current templates/scripts/spec-pipeline.sh:261 instead stop with 25 and needs-human after the checkpoint changes. The earlier entry has not been reconciled; this ADR records the current code without editing that documentation.
