#!/bin/bash
# The merge line in the log names the head the merge is pinned to (v3.1.0-rc.2).
#
# merge-pipeline.sh pins `gh pr merge` to the head its just-in-time gate checked
# (--match-head-commit), but the log said only "Merging PR #233 (squash)...": which commit the
# gates passed and GitHub was asked to merge had to be dug out of GitHub afterwards.
# The line now reads "Merging PR #<n> (<strategy>) at <head>...", printed where
# the merge call is made.
#
# Runs the REAL merge-pipeline.sh (on its own, and inline from the REAL code-review-pipeline.sh)
# with the real gate helpers against the pr2 gh double (tests/lib/pr2-gate.sh); Linear is the
# harness stub, the reviewers fake_claude.sh.
#   1. the merge stage: the line names the PR's head, the same SHA the merge call is pinned to
#   2. a push that lands after the head was pinned and before the line is printed: the line
#      names the head that was checked and pinned, not the one that arrived; nothing is merged
#   3. the review stage's inline merge (merge agent off) prints the same line
# Negative control: against v3.1.0-rc.1 (5184cf8) the line carries no SHA and case 1 fails.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP
ISSUE=EXP-803
trap 'teardown || true' EXIT
fail() { echo "FAIL: $1" >&2; printf '%s\n--- stderr ---\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }

new_sandbox() {
  teardown || true
  sandbox_init "$ISSUE" test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
  pr2_gate_setup
  jq -n '[{createdAt: "2026-09-29T09:00:00Z", body: "## Code Review v2 — EXP-803\n\n**Verdict**: APPROVE"}]' > "$PR2_GH/comments.json"
  HEAD_SHA=$(jq -r .headRefOid "$PR2_GH/pr.json")
  [ "${#HEAD_SHA}" = 40 ] || fail "no PR head in the double"
}
pinned() { awk -F'\t' '$2 == "pr" && $3 == "merge" { for (i = 4; i < NF; i++) if ($i == "--match-head-commit") print $(i + 1) }' "$SANDBOX/gh_calls.log"; }

# ── 1. the merge stage ─────────────────────────────────────────────────────────────────
new_sandbox
run_pipeline merge-pipeline.sh "$ISSUE"
[ "$LAST_RC" = 0 ] && pr2_merged || fail "1: the merge stage did not merge (rc $LAST_RC)"
grep -qxF "  Merging PR #99 (squash) at $HEAD_SHA..." <<< "$LAST_STDOUT" || fail "1: the merge line does not name the head $HEAD_SHA"
[ "$(pinned)" = "$HEAD_SHA" ] || fail "1: the merge was pinned to '$(pinned)', the log names $HEAD_SHA"
[ "$(grep -c '^  Merging PR #' <<< "$LAST_STDOUT")" = 1 ] || fail "1: the merge line is not printed exactly once"
echo "PASS 1 the merge line names the head the merge call is pinned to"

# ── 2. a push between the gate and the merge call ──────────────────────────────────────
new_sandbox
NEW_SHA=1111111111111111111111111111111111111111
# The push lands right after the second read of the head (the gate's, then the one the merge
# is pinned to), before the merge line is printed: a line that read the head again would
# name the new one.
printf '2 %s' "$NEW_SHA" > "$PR2_GH/move_head_after_reads"
run_pipeline merge-pipeline.sh "$ISSUE"
pr2_merged && fail "2: merged a head no gate has seen"
[ "$LAST_RC" = 2 ] || fail "2: a moved head ended $LAST_RC, wanted 2"
grep -qxF "  Merging PR #99 (squash) at $HEAD_SHA..." <<< "$LAST_STDOUT" || fail "2: the merge line does not name the checked head"
grep -qF "at $NEW_SHA" <<< "$LAST_STDOUT" && fail "2: the merge line names the head that arrived after the gate"
grep -qF "The PR head moved from $HEAD_SHA to $NEW_SHA" <<< "$LAST_STDOUT" || fail "2: the move is not reported"
echo "PASS 2 a push after the pin: the line names the checked and pinned head, nothing merged"

# ── 3. the inline merge of the review stage ────────────────────────────────────────────
new_sandbox
cat > "$SANDBOX/approve.txt" <<'EOF'
Review checked.
```json
{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"Checks passed"}
```
EOF
export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt"
export BUREAU_STUB_ISSUE_STATE='Build Review' BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
echo '[]' > "$PR2_GH/comments.json"
run_pipeline code-review-pipeline.sh "$ISSUE"
[ "$LAST_RC" = 0 ] && pr2_merged || fail "3: the review's inline merge did not merge (rc $LAST_RC)"
grep -qxF "  Merging PR #99 (squash) at $HEAD_SHA..." <<< "$LAST_STDOUT" || fail "3: the inline merge line does not name the head"
[ "$(pinned)" = "$HEAD_SHA" ] || fail "3: the inline merge was pinned to '$(pinned)'"
echo "PASS 3 the review stage's inline merge prints the same line"

