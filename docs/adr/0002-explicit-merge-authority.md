# 0002 Explicit human merge authority and one automated merge gate

- **Status**: accepted (derived)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `8fdde36` (manual mode refuses without a Merge state and disables dispatch); `docs/release-notes-v3.0.0.md:15` (released merge policy); `docs/configuration.md:76` (mode and defaults); `docs/configuration.md:78` (CI/base gates); `docs/stage-protocol.md:86` (stop and app boundaries); `templates/scripts/merge-pipeline.sh:589` (final gate rerun and pinned merge); `templates/scripts/bureau-config.sh:2037` (actual PR-base check); `templates/scripts/bureau-charter.py:2` (explicit shared-Git charter authority).

## Context

A review APPROVE is an input to a merge decision. An adopter may reserve merging and rebasing for a human, while another may authorize automation. Cached mergeability alone can overlook red CI or a base that advanced after review.

## Options considered

- Let every APPROVE merge immediately for low latency, accepting stale-base and CI races and removing the human boundary.
- Require humans to merge in every installation, preserving universal human control but removing the existing automated workflow.
- Configure manual or auto authority explicitly and route every automated merge through one gate, accepting extra checks and waiting states.

## Decision

Honor agents.merge_mode: manual disables merge and rebase before external work, cannot be overridden by forced dispatch, requires a Merge state and parks an approved ticket there for a human. Invalid modes fall closed to manual; absent/null remains the released auto default. Sources: `8fdde36`; `docs/configuration.md:76`.

In auto mode, route inline review merges and dedicated Merge workers through the same merge pipeline, independently check CI and the current PR base, rerun the gates immediately before merging and pin the merged head. App finish and an outside green test do not execute or authorize a merge. Sources: `templates/scripts/merge-pipeline.sh:589`; `docs/configuration.md:78`; `docs/stage-protocol.md:86`.

Explicit shared-Git charter enrollment adds policy-integrity conditions to that same automated gate. Resolve the actual default branch, pin commit D and read its `.bureau.json` directly from that commit, independently of checkout discovery. Revalidate D at the merge evaluations. An enrolled unreadable/invalid policy or a PR changing `.bureau.json` requires a human exception, exit 25, even when its approval needs routine re-review. A checkout flag cannot remove enrollment; only the maintainer's explicit unenroll command does. Unenrolled merge runs add exactly one local Git read, `bureau-charter.py merge-common-dir`, which runs from the working directory with every inherited `GIT_*` variable removed and hooks off, and no GitHub CLI or network call; a bare repository or a missing work tree is indeterminate. Cache that result in an unexported shell variable for the initial probe and both gate passes, passing it explicitly to `status --common-dir`; the probe only validates enrollment and its parents, while Git decides repository authority. An indeterminate lookup or record remains `charter-policy` with exit 25. This foundation defines queue, selector, suite, red-main and report configuration without enforcing those future gates. Sources: `templates/scripts/bureau-charter.py:2`; `templates/scripts/merge-pipeline.sh`; [configuration](../configuration.md#merge-charter-foundation).

## Rationale

Authority must survive dispatch overrides and configuration mistakes. Checking independently of cached mergeability and repeating checks at publication addresses the observed race directly. Configurable authority reflects the released product; a universal manual default would be a new decision. Reusing an earlier shell result is unavailable at the merge probe: explicit `BUREAU_CONFIG` skips `_find_config`, and the earlier evidence lookups run in Python children without returning the directory to the shell. Both scanner call baselines therefore deliberately include the single local lookup, keeping the other calls and GitHub order unchanged. Delegating layout resolution to Git avoids maintaining an incomplete second repository detector.

## Consequences

Manual installations need a configured Merge state and an operator to complete the human handoff. Automated workflows may wait or halt after approval. Merge-policy exceptions for repositories without CI require deliberate configuration and are not a debugging shortcut.

## Known limits

Manual GitHub operations are outside Bureau's automated gate. A default-auto configuration permits the inline merge path even when the dedicated merge agent is disabled. Local tests and reviews do not substitute for the gate's current remote checks, and no gate can prevent all external changes after its final observation.

Charter enrollment is a local accident guard shared by linked worktrees, not an authorization boundary against another process with the same user permissions. Separate clones do not share it. Run trusted installed scripts; a PR-controlled script is not an authority. Doctor checks only locally fetched default-branch policy and static declarations. Suite execution, durable reports and main-CI latches are not provided by this foundation.

The opt-in exact-commit approval workflow is an accident guard against approval being carried silently to a later push. It records `bureau/human-approval` for an allowed login's `/bureau approve <40-hex-sha>` comment on the current head, using `agents.merge_approvers` read from the default branch. The comment trigger runs the default branch's workflow file and checks out only that branch. A new push leaves its new commit without the status until someone approves it. Bureau's pipelines never invoke the writer. Branch protection must require the context, which Bureau does not inspect or change. This does not stop deliberate bypass: other PR workflows can write the same status, and Bureau agents using an allowed person's login can approve themselves. An unforgeable form needs a separate Bureau identity and is a later change. This extends the existing human-authority decision without changing merge modes or the merge gate; see `templates/scripts/bureau-approval-status.py`, `templates/.github/workflows/bureau-approval.yml` and [the recipe](../recipes.md#require-a-persons-approval-of-the-exact-commit).

## Reversal condition

Replace this decision only if the authority default or available modes change deliberately, or a different merge mechanism demonstrably preserves the human boundary and equivalent current-head/current-base checks.

## Evidence

- `tests/test_merge_mode.sh` exercises manual-mode refusal, forced dispatch and missing Merge-state behavior.
- `tests/test_merge_pipeline_correctness.sh` exercises red/pending CI, stale bases, just-in-time races and head-pinned merge calls.
- `tests/test_inline_merge_result.sh` checks that review acts on the shared merge pipeline's actual result.
- `tests/test_merge_gate_switches.sh` exercises the explicit CI/base configuration exceptions.
- `tests/test_merge_charter.sh` defines real-Git trusted-policy, enrollment, doctor drift, disabled compatibility and merge-refusal cases with enforcement-removal controls; execution belongs to the maintainer's scheduled test slot.

## Open points

docs/configuration.md:79 describes the up-to-date gate as comparison with origin/main. templates/scripts/bureau-config.sh:2037 instead resolves the PR's actual baseRefName and compares that branch's current commit; stacked PRs need the latter distinction. This wording disagreement remains unresolved here.
