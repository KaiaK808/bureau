# 0003 Metadata-only evidence with mandatory success receipts

- **Status**: proposed (v3.5, PR #78)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `4617098` (v3.5 PR 3 evidence contract); `CHANGELOG.md:31` (receipts, manifests and waits); `docs/stage-protocol.md:114` (shared store and attempt identities); `docs/stage-protocol.md:118` (mandatory versus observational writes); `docs/stage-protocol.md:126` (privacy projection); `docs/configuration.md:324` (retention and capacity).

## Context

A provider can finish useful work while stdout forwarding or a later wrapper fails. Operators also need stage identity and waiting time across retries. Copying transcripts, ticket prose or command output into another durable store creates privacy and capacity costs without proving that a different attempt succeeded.

## Options considered

- Rely on transient stdout and stage logs, minimizing storage but losing completion identity during forwarding failures and cleanup.
- Copy full logs and structured results into a durable store, improving self-contained recovery at the cost of duplicated private content and unbounded growth.
- Retain allowlisted metadata, digests and original-file pointers, preserving attempt identity with bounded storage while requiring originals for result recovery.

## Decision

Store version-1 receipts, manifests, gate measurements and waits under the shared Git directory's private bureau/evidence/v1 directory using allowlisted metadata and digests; retain no copied logs, excerpts, command text or structured-result prose in those records. ADR 0012 records the separately bounded advisory-insight exception introduced by PR #82. Sources: `4617098`; `docs/stage-protocol.md:114`; `docs/stage-protocol.md:126`.

Persist a successful provider receipt before forwarding success. Recovery must match the same run, provider attempt, stage attempt, ticket, phase, schema, result digest and current HEAD and must verify the unchanged original result. Other evidence I/O is observational and cannot promote a failed, cancelled or timed-out attempt. Sources: `CHANGELOG.md:31`; `docs/stage-protocol.md:118`.

## Rationale

The store preserves identity and integrity checks without becoming another prose archive. Making success persistence mandatory closes the stdout-loss window; making other writes best effort prevents optional diagnostics from changing stage authorization or exits.

## Consequences

Linked worktrees share locking and evidence; other clones do not. New directories use 0700 and files 0600, with locked atomic writes. Retention limits completed data to 128 evictable runs/30 days under a global 16 MiB and 4,096-file budget including staging. Active/interrupted runs and open waits are protected, so capacity exhaustion may require operator inspection. Resync scripts together and inspect/remove an older raw-copy store before adopting this contract; resync does not scrub historical bytes.

## Known limits

Missing or changed originals prevent completion recovery. Pointers can disclose local locations to an operator and remain private to the store. The lock is neither cross-host coordination nor tamper-proof attestation. Abrupt termination and unusual filesystem failures remain outside the tested guarantees. Waiting statistics cover retained observations, not full historical elapsed time; switching sweep selection modes can require one extra sweep.

## Reversal condition

Revisit if operators require self-contained archival recovery or cross-host evidence. Such a change needs an explicit privacy/retention design and a new compatibility contract rather than silently retaining prose.

## Evidence

- `tests/provider_adapter_test.py`, invoked by `tests/test_provider_adapter.sh`, exercises exact-attempt receipts, original-result recovery and refusal to promote unsuccessful attempts.
- `tests/test_evidence_negative_controls.sh` mutation-checks receipt identity, privacy, optional-write behavior and capacity protections.
- `tests/test_shared_runtime.sh` covers manifests, waits, protected retention and observational I/O failures through real runtime wrappers.

## Open points

No core metadata-only disagreement was found after accounting for the explicit PR #82 insight exception in docs/stage-protocol.md:126. A representative live provider and multi-installation rollout remains pending; the stack's fixture qualification is not that acceptance.
