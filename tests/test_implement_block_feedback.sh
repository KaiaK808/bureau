#!/bin/bash
# A restart from build after a build review BLOCK hands the review's findings to implement
# (v3.2).
#
# A BLOCK posts "🚫 Code review **BLOCKED** — needs human review." with the merged review
# (code-review-pipeline.sh), labels needs-human and ends the stage with 25. An operator who
# removes the label and restarts with `shepherd.sh --from-stage build` sends the ticket to
# implement, which reads review feedback only through refresh_review_context
# (implement-pipeline.sh). Before v3.2 its filter took the newest "Code Review … Changes
# Requested", FIXES_NEEDED or `VERDICT: REQUEST_CHANGES` comment; the BLOCK matched none of
# them, so the build stage ran without the review's findings unless someone had written a
# FIXES_NEEDED comment by hand.
#
# The REAL review stage runs in the harness (doubles: the Linear stub, the fake models, the
# pr2 `gh`) once to a BLOCK and once to a REQUEST_CHANGES, and the Linear comments it posted
# are taken byte for byte. The REAL implement stage then runs over a ticket that carries
# them, read through the REAL get_issue_branch_and_comments (it sorts newest first) from a
# Linear double at the GraphQL call (linear_issue_query, answer oldest first). The test
# reads the prompt the agent got:
#   a. a BLOCK (and the shepherd's halt comment after it) → the prompt carries its findings
#   b. a BLOCK, then an operator's FIXES_NEEDED → the operator's list, the BLOCK is not used
#   c. an older Changes Requested, then a newer BLOCK → the BLOCK, not the older review
#   d. no review comment → no feedback block, as before
#   e. a BLOCK, then a note quoting the BLOCK heading on a line of its own → the BLOCK
#      (only a comment that starts with the heading is a BLOCK)
# Every case runs and the failures are listed at the end. Negative control: against the
# implement stage of 0153ccf (and v3.1.0, the same filter) a, c and e fail: the BLOCK is
# never read; b and d pass there too.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP BUREAU_DRY_RUN
ISSUE=EXP-806
ORIG_PATH="$PATH"
fail() { echo "FAIL: $1" >&2; printf '%s\n--- stderr ---\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }
FAILED=""
check_fail() { echo "FAIL $1" >&2; FAILED="$FAILED $2"; }

# The heading every release since v3.0.0 posts on a BLOCK. BLOCK comments already on
# tickets carry these bytes, so implement must keep finding them: a review stage that posts
# another heading keeps this one in implement's filter as well.
V31_BLOCK_HEADING='🚫 Code review **BLOCKED** — needs human review.'

# ── the review stage posts the comments ────────────────────────────────────
# review_comment <merger JSON> <expected exit> — the REAL review stage; sets REVIEW_BODY to
# the last Linear comment it posted, exactly as passed to post_comment.
review_comment() {
  teardown || true
  PATH="$ORIG_PATH"
  sandbox_init "$ISSUE" test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt && git -C "$SANDBOX" commit -q -m 'fixture change' && git -C "$SANDBOX" push -q origin test-branch
  printf 'Specialist prose.\n```json\n{"specialist":"x","counts":{"critical":0,"bug":0,"minor":0,"skip":0},"findings":[],"summary":""}\n```\n' > "$SANDBOX/specialist.txt"
  printf '%s\n' "$1" > "$SANDBOX/merger.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/specialist.txt" FAKE_CLAUDE_MERGE_FIXTURE="$SANDBOX/merger.txt"
  export BUREAU_STUB_ISSUE_STATE='Build Review' BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  pr2_gate_setup
  # The stub records a comment in calls.log, where a multi-line body cannot be told from the
  # next record; this keeps every body whole as well.
  cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
post_comment() { _record post_comment "$1" "$2"; jq -cn --arg b "$2" '{body: $b}' >> "$SANDBOX/posted.jsonl"; }
EOF
  run_pipeline code-review-pipeline.sh "$ISSUE"
  [ "$LAST_RC" = "$2" ] || fail "the review stage ended $LAST_RC, wanted $2"
  [ -s "$SANDBOX/posted.jsonl" ] || fail 'the review stage posted no Linear comment'
  REVIEW_BODY=$(tail -1 "$SANDBOX/posted.jsonl" | jq -r .body)
}

merger() {  # <verdict> <file> <line> <message> <fix> — a merger answer in the review schema
  jq -nc --arg v "$1" --arg f "$2" --argjson l "$3" --arg m "$4" --arg x "$5" '{verdict: $v, bugs: 1, security_issues: 0,
    missing_acceptance: [], fixes_needed: [$x], summary: "One bug.",
    comment: ("## Specialist Summaries\n- Correctness: one bug.\n\n## Findings\n### BUG\n- `" + $f + ":" + ($l | tostring) + "` — " + $m + ".\n\n## Fixes Needed\n1. " + $x + "."),
    findings: [{file: $f, line: $l, class: "BUG", msg: $m}]}'
}

BLOCK_FINDING='`src/b.py:7` — the token is used before it is validated.'
review_comment "$(merger BLOCK src/b.py 7 'the token is used before it is validated' 'Validate the token before use')" 25
BLOCK="$REVIEW_BODY"
grep -qF -- "$BLOCK_FINDING" <<< "$BLOCK" || fail "the BLOCK comment lacks the finding: $BLOCK"
[ "${BLOCK%%$'\n'*}" = "$V31_BLOCK_HEADING" ] \
  || fail "the review stage's BLOCK comment no longer starts with the v3.1.0 heading: ${BLOCK%%$'\n'*}"
RC_FINDING='`src/a.py:12` — the loop reads past the end of an empty list.'
review_comment "$(merger REQUEST_CHANGES src/a.py 12 'the loop reads past the end of an empty list' 'Guard the empty list')" 0
CHANGES="$REVIEW_BODY"
grep -qF -- "$RC_FINDING" <<< "$CHANGES" || fail "the Changes Requested comment lacks the finding: $CHANGES"
grep -q $'^move_issue\t'"$ISSUE"$'\tstate-build$' "$SANDBOX/calls.log" || fail 'REQUEST_CHANGES did not go back to Build'
echo 'PASS the review stage posted a BLOCK and a Changes Requested comment with their findings'

# ── the implement stage reads them ─────────────────────────────────────────
MARKER='<!-- bureau-branch: test-branch -->
**Spec Artifacts — EXP-806**'
HALT='🐑 Shepherd halt: `needs-human` label present at `Build Review`. The stage that just ran flagged this ticket for human review; shepherd will not re-run it. Remove the label and re-shepherd when ready.'
OPERATOR='FIXES_NEEDED — the operator'"'"'s list for the next build pass

1. Validate the token in the request handler; the second finding of the review is overruled.'
QUOTE='Removed needs-human after this review:
🚫 Code review **BLOCKED** — needs human review.
Restarting from build; the findings above stand.'
SPEC_PASS='✅ Spec review **PASSED**. Ready for implementation.

The artifacts match the repo.'
FEEDBACK_HEAD='--- Code Review Feedback (PRIORITY) ---'

# implement_prompt <comment>... — the REAL implement stage on a ticket with the branch marker
# and these comments, oldest first, a minute apart; sets PROMPT to what the agent was told.
implement_prompt() {
  teardown || true
  PATH="$ORIG_PATH"
  unset FAKE_CLAUDE_MERGE_FIXTURE BUREAU_STUB_STATE_MERGE BUREAU_STUB_AGENT_ENABLED BUREAU_NO_MERGE BUREAU_STOP_REQUESTED PR2_GH PR2_NOW
  sandbox_init "$ISSUE" test-branch
  # The real reader (newest first, branch from the marker) over a Linear double at the
  # GraphQL call; the double holds its answer to the shape the real fetch requires.
  sed -n -e '/^_BUREAU_SHAPE_ISSUES=/p' -e '/^_BUREAU_SHAPE_ISSUE_COMMENTS=/p' \
    -e '/^get_issue_branch_and_comments() {/,/^}/p' \
    "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  grep -q '^get_issue_branch_and_comments() {' "$SCRIPTS_DIR/bureau-config.sh" || fail 'real get_issue_branch_and_comments not found'
  cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
linear_issue_query() {
  _record linear_issue_query
  jq -e "$2" "$SANDBOX/linear-answer.json" >/dev/null || { echo "linear double: the answer fails the reader's shape" >&2; return 27; }
  cat "$SANDBOX/linear-answer.json"
}
EOF
  jq -n '$ARGS.positional | to_entries
    | map({body: .value, createdAt: ("2026-10-05T10:" + ((.key + 10) | tostring) + ":00.000Z")})
    | {data: {issues: {nodes: [{branchName: "test-branch", comments: {nodes: .}}]}}}' \
    --args "$MARKER" "$@" > "$SANDBOX/linear-answer.json"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS=1
  export FAKE_CLAUDE_PROMPT_LOG="$SANDBOX/prompts.log" BUREAU_IMPL_MAX_ITER=1 BUREAU_STUB_ISSUE_STATE=Build
  run_implement_pipeline "$ISSUE"
  [ "$LAST_RC" = 0 ] || fail "the implement stage ended $LAST_RC"
  grep -q '^linear_issue_query' "$SANDBOX/calls.log" || fail 'the implement stage never read the comments through the double'
  PROMPT=$(cat "$SANDBOX/prompts.log" 2>/dev/null || true)
  [ -n "$PROMPT" ] || fail 'the implement stage never called the agent'
  # The feedback block alone, so a check cannot be fooled by text elsewhere in the prompt.
  FEEDBACK=$(awk -v h="$FEEDBACK_HEAD" '$0 == h {on=1; next} $0 == "--- End feedback ---" {on=0} on' <<< "$PROMPT")
}

has() { grep -qF -- "$1" <<< "$FEEDBACK"; }

# a. a BLOCK, then the shepherd's halt comment
implement_prompt "$SPEC_PASS" "$BLOCK" "$HALT"
if has "$BLOCK_FINDING" && has "$V31_BLOCK_HEADING"; then
  echo 'PASS a: after a BLOCK the prompt carries the BLOCK and its findings'
else
  check_fail "a: the prompt carries no BLOCK findings; feedback block: [${FEEDBACK:0:300}]" a
fi

# b. a BLOCK, then the operator's FIXES_NEEDED
implement_prompt "$SPEC_PASS" "$BLOCK" "$HALT" "$OPERATOR"
if has "the operator's list for the next build pass" && ! has "$BLOCK_FINDING"; then
  echo 'PASS b: an operator FIXES_NEEDED written after the BLOCK is what the prompt carries'
else
  check_fail "b: wanted the operator's FIXES_NEEDED alone; feedback block: [${FEEDBACK:0:300}]" b
fi

# c. an older Changes Requested, then a newer BLOCK
implement_prompt "$SPEC_PASS" "$CHANGES" "$BLOCK" "$HALT"
if has "$BLOCK_FINDING" && ! has "$RC_FINDING"; then
  echo 'PASS c: a BLOCK newer than a Changes Requested is what the prompt carries'
else
  check_fail "c: wanted the newer BLOCK alone; feedback block: [${FEEDBACK:0:300}]" c
fi

# d. no review comment
implement_prompt "$SPEC_PASS" '🎨 Design phase complete. Ready for implementation.'
if ! grep -qF -- "$FEEDBACK_HEAD" <<< "$PROMPT"; then
  echo 'PASS d: without a review comment the prompt has no feedback block'
else
  check_fail "d: a feedback block without a review comment: [${FEEDBACK:0:300}]" d
fi

# e. a BLOCK, then a note that quotes its heading on a line of its own
implement_prompt "$SPEC_PASS" "$BLOCK" "$HALT" "$QUOTE"
if has "$BLOCK_FINDING" && ! has 'Restarting from build; the findings above stand.'; then
  echo 'PASS e: a note quoting the BLOCK heading is not taken for the BLOCK'
else
  check_fail "e: wanted the BLOCK, not the note quoting it; feedback block: [${FEEDBACK:0:300}]" e
fi

[ -z "$FAILED" ] || { echo "FAILED cases:$FAILED" >&2; exit 1; }
echo 'OK test_implement_block_feedback'
