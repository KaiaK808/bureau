# 0008 Reuse preserved implement workers only for proven clean base merges

- **Status**: proposed (v3.5, PR #80)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `5a25c7e` (merge-only worker reuse); `docs/release-notes-v3.4.0.md:25` (released clean on-origin reuse); `docs/stage-protocol.md:22` (retained worker policy); `docs/stage-protocol.md:24` (base selection); `docs/stage-protocol.md:28` (eligible preservation records); `templates/scripts/bureau-config.sh:2731` (ahead commits exclude pinned base history); `templates/scripts/bureau-config.sh:2780` (re-acceptance classifier); `templates/scripts/bureau-worker.sh:64` (spec-worker exception); `docs/provider-runtime.md:45` (older general failure description).

## Context

A failed implement call can leave a clean worktree ahead of origin only because the executor merged the base for validation. The released clean-on-origin rule rejects that state. Resetting loses the merge; treating every ahead worktree as disposable risks erasing actual implementation or conflict-resolution work.

## Options considered

- Refuse every unpublished commit, protecting work but requiring manual recovery for deterministic clean merges.
- Reset any clean-looking worktree, making retries easy but deleting meaningful commits and potentially racing user-owned work.
- Retain only a precisely identified merge-only implement state, accepting extra Git verification to preserve safe retry progress.

## Decision

Keep existing clean equal/behind-origin re-acceptance independent of base resolution. Additionally re-accept the same linked implement worktree only when its unfinished preservation record matches issue, branch and workspace with exit 22 or 23, and every ahead commit outside the pinned base history is a verified clean two-parent merge of the pinned resolved base. Require exact merge-tree equality and unchanged refs/status; retain HEAD without reset or clean. Sources: `5a25c7e`; `docs/stage-protocol.md:22`; `docs/stage-protocol.md:28`; `templates/scripts/bureau-config.sh:2731`; `templates/scripts/bureau-config.sh:2780`.

Resolve the merge-only base from a valid configured repo.base_branch, otherwise symbolic origin/HEAD, otherwise origin/main; an unresolvable selected ref refuses this new reuse path. Spec workers instead retain ordinary disposable registration and reset to verified committed checkpoints or the origin base. Interrupted runs retain their explicit ownership recovery requirement. Sources: `docs/stage-protocol.md:24`; `docs/stage-protocol.md:28`; `templates/scripts/bureau-worker.sh:64`.

## Rationale

A clean working tree alone says nothing about the meaning of unpublished commits. Verifying ancestry and the committed merge tree admits the deterministic base merge without admitting own commits, manual resolutions or extra changes. The preservation record limits this exception to the failures it was designed to retry.

## Consequences

Git 2.38 or later is needed for merge-tree verification. Reuse must target the original worktree path; a different path cannot take a branch still held there. Legacy records without exit codes, timeouts and unknown failures require inspected recovery. Failing implementation still attempts its normal merge-only push, which can produce a CI run. Resync shared config, worker and implement scripts together.

## Known limits

Ignored files do not count as dirt and survive retained preparation. The rule never authorizes resetting an unregistered user checkout or stealing another branch holder. Ref/fetch/process uncertainty fails closed, and interruption 130 remains outside automatic merge-only recovery. The base-selection extension does not change the base policy of every other stage.

## Reversal condition

Revisit if verified merge-only states cannot be distinguished reliably on supported Git versions, or if a new preservation reason needs automatic reuse. Add an explicit classifier and preservation contract before widening acceptance.

## Evidence

- `tests/test_implement_merge_worker.sh` exercises two real worker ticks, retained HEAD and rejection of own commits and conflict resolutions, with negative controls.
- `tests/test_merge_reuse_base.sh` exercises custom, no-main, symbolic and unresolved base cases.
- `tests/test_reaccept_clean_worktree.sh` covers the released equal/behind-origin rule.
- `tests/test_spec_resume.sh` exercises the distinct committed-checkpoint reset rule for spec workers.

## Open points

docs/provider-runtime.md:45 broadly says a failed worker with uncommitted or unpublished progress loses disposable registration. templates/scripts/bureau-worker.sh:64 and docs/stage-protocol.md:28 now exempt ordinary spec failures so they can reset to committed phase checkpoints. The general provider paragraph has not been updated to state that exception.
