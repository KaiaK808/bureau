<!-- bureau-acceptance: pr=1 head={head} -->
## Acceptance brief — {issue} / PR #1
Awaiting human decision. This brief does not establish acceptance, merge or freshness.

**Intent**
- Spec intent: missing.

**Result**
- HEAD: [{head}](https://example.invalid/repo/commit/{head})
- Change: 3 files, 1 commits; [diff](https://example.invalid/repo/compare/{base}...{head}).
- Diff size: 3 files, 11 insertions, 1 deletions, 0 binary files (base..HEAD; renames counted as delete/add).
- declared files: missing.
- Risk tier: normal; 3 files, 12 changed lines, 0 binary files; docs/test paths=1/3, sensitive paths=0; low limits: files <= 5, changed lines <= 100; high threshold: 1000 changed lines.
- Risk tier is advisory only: it changes no routing, holds, verdicts or merges. A human merges every PR.
- CI status: unavailable (HEAD={head}).

**Stage verdicts**
- spec: missing.
- spec_review: missing.
- ux: missing.
- copy: missing.
- implement: missing.
- qa: missing.
- code_review: missing.
- merge: missing.
- rebase: missing.

**Test evidence**
- qa.test: exit=0, HEAD={head}, end_HEAD={head} (matches brief HEAD), sha256={gate-sha256}; `run {run}/manifest`, attempt `{attempt}`, gate 0
- qa.test: exit=0, HEAD={head}, end_HEAD={head} (matches brief HEAD), sha256={gate-sha256}; `run {run}/manifest`, attempt `{attempt}`, gate 1

**Review evidence**
- Final verdict: missing; REVIEW_HEAD=missing.
- Review rounds used: 0 recorded for this PR; cap=4 (effective max_review_rounds).
- Review receipt/verdict for this HEAD: missing.

**Token usage**
- Tokens spent: missing (cost tracking off).

**Open points**
- Review blockers/known limits: missing.
- Wait records: missing (absence is not proof of no waits).
- Other blockers/known limits: missing; confirm with the owner.

**Evidence index**
- `run {run}/manifest`
- Operator store location: `<git common dir>/bureau/evidence/v1`.
- Local evidence is observational, may expire, and is not accessible from GitHub without the repository evidence store.
