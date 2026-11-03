# Read-only Bureau PR review brief

Maintainer: copy this brief, fill in the inputs and replace the ADR list with the records relevant to this diff. Find candidates through the number, title and Scope columns in [the ADR index](../docs/adr/README.md), then read each record's scope in Context and Decision, any explicit Applies to section, and Known limits. Read [the ADR template](../docs/adr/0000-template.md) for the actual fields: Status, Date, Decided by and Sources, followed by Context, Options considered, Decision, Rationale, Consequences, Known limits, Reversal condition, Evidence and Open points. Scope is currently expressed in those sections rather than a required Applies to field.

## Inputs

- PR: `<URL or number>`
- Repository/worktree: `<path>`
- Pinned base: `<commit ID>`
- Pinned head: `<commit ID>`
- Review focus and explicit exclusions: `<scope>`
- Relevant ADRs, by number **and title**, including status and verified authority:
  - `ADR NNNN — Full decision title — accepted (derived) — settled (same path and blob ID on base/head)`
  - `ADR NNNN — Full proposal title — accepted — proposal under review (added/changed/removed by this PR)`
  - `<additional relevant records; remove this placeholder>`
- Checks already run and their exact results: `<commands and outcomes>`

## Reviewer instructions

Review the pinned diff and trace any suspected defect through its callers, persistence and error paths. This is a read-only external review: do not edit files, commit, push, open a PR, change remote state or launch live provider/service calls. Use local checks with fake services where appropriate. Report a tooling restriction as missing validation evidence, with its command and result.

Read the listed ADRs at the pinned head, or at the pinned base for a removed record. Only accepted ADRs unchanged from the target branch count as settled: verify that both path and blob ID are identical on pinned base and pinned head. A finding that contradicts a settled ADR must explicitly name it (for example `ADR 0003`) and state the new evidence that justifies reopening it, considering its Reversal condition. Without that evidence the disagreement is out of scope; it may only be noted separately as an ADR change proposal. Only a settled ADR's documented known limit is excluded as a finding.

ADRs added, changed or removed by the PR are proposals under review, not authoritative, even when their Status says accepted. A finding may validly reference a proposal, but it carries no settled status and never waives or downgrades any finding, including a claimed known limit. Other proposed records are context; superseded records do not override their replacements.

Use the evidence discipline in [ADR 0011](../docs/adr/0011-independent-provider-review.md) and [the review protocol](../docs/stage-protocol.md#review-convergence): independent inspection of pinned inputs, concrete evidence and explicit validation limits. Separate a violated decision or new regression from an intentional documented boundary. Cite ADR numbers without copying their bodies into review evidence.

## Required response

Number substantive findings in descending severity. Use P1 (urgent correctness/security/data-loss issue), P2 (material defect requiring rework) or P3 (minor actionable issue). There is no findings quota; a normal focused review may have zero findings. For each finding provide:

1. `[P2] Short description` — the concrete trigger, resulting behavior and impact.
   Evidence: `path:line` at the pinned head, or the exact local command and observed result. Cite the relevant `ADR NNNN`; if contradicting it, give the new evidence for reopening it.
   Suggested correction: the smallest change that addresses the demonstrated defect.

After the findings, state validation performed and any remaining uncertainty. Put optional ADR change proposals in a separate paragraph; they do not count as defects or independently change the verdict. End with exactly one verdict line:

`Verdict: APPROVE | REQUEST_CHANGES | BLOCK`

Choose one value: APPROVE when no substantive blocker is established, REQUEST_CHANGES for demonstrated defects needing rework, or BLOCK for a security/data-loss blocker or an unresolved decision requiring a maintainer. The verdict explains this review; it grants no merge or publication authority.
