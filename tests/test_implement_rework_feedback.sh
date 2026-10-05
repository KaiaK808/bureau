#!/bin/bash
# A rework reads the newest review or QA findings (v3.2).
#
# Implement reads the findings it should fix only through refresh_review_context
# (implement-pipeline.sh). Before v3.2 its filter took the newest "Code Review … Changes
# Requested", FIXES_NEEDED or `VERDICT: REQUEST_CHANGES` comment, so three kinds of findings
# never reached it:
#   - QA RED, "🔄 QA: tests failing — routing back to Build." (qa-pipeline.sh), which sends
#     the ticket back to Build by itself: every QA RED rework ran without QA's summary;
#   - a build review BLOCK, "🚫 Code review **BLOCKED** — needs human review."
#     (code-review-pipeline.sh), and
#   - QA NEEDS_HUMAN, "🚫 QA flagged for human review." (qa-pipeline.sh), both of which end
#     with needs-human: an operator's restart with `shepherd.sh --from-stage build` ran without
#     the findings unless someone had written a FIXES_NEEDED comment by hand.
# (The app runtime's `VERDICT: BLOCK` is covered in tests/shared_runtime_test.py.)
#
# The REAL review stage (to a BLOCK and to a REQUEST_CHANGES) and the REAL QA stage (to RED
# and to NEEDS_HUMAN) run in the harness (doubles: the Linear stub, the fake models, the pr2
# `gh`), and the Linear comments they post are kept byte for byte. The REAL implement stage
# then runs over a ticket that carries them, read through the REAL
# get_issue_branch_and_comments (it sorts newest first) from a Linear double at the GraphQL
# call (linear_issue_query, answer oldest first). The test reads the feedback block of the
# prompt the agent got:
#   a. a BLOCK (and the shepherd's halt comment after it) → the BLOCK and its findings
#   b. a BLOCK, then an operator's FIXES_NEEDED → the operator's list, not the BLOCK
#   c. an older Changes Requested, then a newer BLOCK → the BLOCK, not the older review
#   d. no review or QA comment → no feedback block, as before
#   e. a BLOCK, then a note quoting all three headings, each on a line of its own → the
#      BLOCK (only a comment that starts with a heading counts)
#   f. QA RED → QA's summary, under a label that names no code review
#   g. QA RED, then QA PASSED and a newer Changes Requested → the review, not QA
#   h. a Changes Requested, then a newer QA RED → QA, not the older review
#   i. QA RED, then an operator's FIXES_NEEDED → the operator's list
#   j. QA NEEDS_HUMAN (and the shepherd's halt comment) → QA's summary
#   k. the QA bounce: QA wrote and ticked the tests, then RED; the agent finds every
#      task checked and reports COMPLETE without a commit → the stage still hands the ticket
#      on to QA without needs-human, as before, and the prompt now carries QA's summary
# Every case runs and the failures are listed at the end. Negative control: against the
# implement stage of 0153ccf (and v3.1.0, the same filter) a, c, e, f, h, j and k's prompt
# fail; b, d, g, i and k's hand-off pass there too.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP BUREAU_DRY_RUN
ISSUE=EXP-806
ORIG_PATH="$PATH"
fail() { echo "FAIL: $1" >&2; printf '%s\n--- stderr ---\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }
FAILED=""
check_fail() { echo "FAIL $1" >&2; FAILED="$FAILED $2"; }

# The headings every release since v3.0.0 posts. Comments already on tickets carry these
# bytes, so implement must keep finding them: a stage that posts another heading keeps this
# one in implement's filter as well.
V31_BLOCK_HEADING='🚫 Code review **BLOCKED** — needs human review.'
V31_QA_RED_HEADING='🔄 QA: tests failing — routing back to Build.'
V31_QA_HUMAN_HEADING='🚫 QA flagged for human review.'

# record_comments — make the stub keep every Linear comment whole (calls.log cannot tell a
# multi-line body from the next record).
record_comments() {
  cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
post_comment() { _record post_comment "$1" "$2"; jq -cn --arg b "$2" '{body: $b}' >> "$SANDBOX/posted.jsonl"; }
EOF
}
last_comment() {
  [ -s "$SANDBOX/posted.jsonl" ] || fail "$1 posted no Linear comment"
  STAGE_COMMENT=$(tail -1 "$SANDBOX/posted.jsonl" | jq -r .body)
}

# ── the review stage posts its comments ────────────────────────────────────
# review_comment <merger JSON> <expected exit> — the REAL review stage; sets STAGE_COMMENT.
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
  record_comments
  run_pipeline code-review-pipeline.sh "$ISSUE"
  [ "$LAST_RC" = "$2" ] || fail "the review stage ended $LAST_RC, wanted $2"
  last_comment 'the review stage'
}

merger() {  # <verdict> <file> <line> <message> <fix> — a merger answer in the review schema
  jq -nc --arg v "$1" --arg f "$2" --argjson l "$3" --arg m "$4" --arg x "$5" '{verdict: $v, bugs: 1, security_issues: 0,
    missing_acceptance: [], fixes_needed: [$x], summary: "One bug.",
    comment: ("## Specialist Summaries\n- Correctness: one bug.\n\n## Findings\n### BUG\n- `" + $f + ":" + ($l | tostring) + "` — " + $m + ".\n\n## Fixes Needed\n1. " + $x + "."),
    findings: [{file: $f, line: $l, class: "BUG", msg: $m}]}'
}

BLOCK_FINDING='`src/b.py:7` — the token is used before it is validated.'
review_comment "$(merger BLOCK src/b.py 7 'the token is used before it is validated' 'Validate the token before use')" 25
BLOCK="$STAGE_COMMENT"
grep -qF -- "$BLOCK_FINDING" <<< "$BLOCK" || fail "the BLOCK comment lacks the finding: $BLOCK"
[ "${BLOCK%%$'\n'*}" = "$V31_BLOCK_HEADING" ] \
  || fail "the review stage's BLOCK comment no longer starts with the v3.1.0 heading: ${BLOCK%%$'\n'*}"
RC_FINDING='`src/a.py:12` — the loop reads past the end of an empty list.'
review_comment "$(merger REQUEST_CHANGES src/a.py 12 'the loop reads past the end of an empty list' 'Guard the empty list')" 0
CHANGES="$STAGE_COMMENT"
grep -qF -- "$RC_FINDING" <<< "$CHANGES" || fail "the Changes Requested comment lacks the finding: $CHANGES"
grep -q $'^move_issue\t'"$ISSUE"$'\tstate-build$' "$SANDBOX/calls.log" || fail 'REQUEST_CHANGES did not go back to Build'
echo 'PASS the review stage posted a BLOCK and a Changes Requested comment with their findings'

# ── the QA stage posts its comments ────────────────────────────────────────
# qa_comment <status> <coverage notes> — the REAL QA stage with a red test command and a
# QA agent that answers <status>; sets STAGE_COMMENT.
qa_comment() {
  teardown || true
  PATH="$ORIG_PATH"
  unset FAKE_CLAUDE_MERGE_FIXTURE BUREAU_STUB_STATE_MERGE BUREAU_STUB_AGENT_ENABLED BUREAU_NO_MERGE BUREAU_STOP_REQUESTED PR2_GH PR2_NOW
  sandbox_init "$ISSUE" test-branch
  jq -n '{repo: {test_command: "false"}}' > "$SANDBOX/.bureau.json"
  jq -nc --arg s "$1" --arg n "$2" '{status: $s, tests_added: 1, tests_failing: 1, coverage_notes: $n}' \
    | { printf 'QA prose.\n```json\n'; cat; printf '\n```\n'; } > "$SANDBOX/qa.txt"
  # The stage waits 5 s before it reruns a red suite; nothing here depends on that wait.
  mkdir -p "$SANDBOX/.nosleep"; printf '#!/bin/sh\nexit 0\n' > "$SANDBOX/.nosleep/sleep"; chmod +x "$SANDBOX/.nosleep/sleep"
  PATH="$SANDBOX/.nosleep:$PATH"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/qa.txt" BUREAU_STUB_STATE_QA=state-qa BUREAU_STUB_ISSUE_STATE=QA
  record_comments
  # The stub config has no commit_stage_changes; the QA agent here changes no file, so there
  # is nothing to commit (as tests/lib/pr1-untrusted-env.sh does).
  echo 'commit_stage_changes() { _record commit_stage_changes "$@"; return 0; }' >> "$SCRIPTS_DIR/bureau-config.sh"
  run_pipeline qa-pipeline.sh "$ISSUE"
  [ "$LAST_RC" = 0 ] || fail "the QA stage ended $LAST_RC"
  last_comment 'the QA stage'
  unset BUREAU_STUB_STATE_QA
}

QA_RED_FINDING='tests/test_auth.py::test_expired_token fails: an expired token is still accepted (src/auth.py:41).'
qa_comment RED "$QA_RED_FINDING"
QA_RED="$STAGE_COMMENT"
grep -qF -- "$QA_RED_FINDING" <<< "$QA_RED" || fail "the QA RED comment lacks the summary: $QA_RED"
[ "${QA_RED%%$'\n'*}" = "$V31_QA_RED_HEADING" ] \
  || fail "the QA stage's RED comment no longer starts with the v3.1.0 heading: ${QA_RED%%$'\n'*}"
grep -q $'^move_issue\t'"$ISSUE"$'\tstate-build$' "$SANDBOX/calls.log" || fail 'QA RED did not go back to Build'
QA_HUMAN_FINDING='The new test shows a bug in the implementation: src/auth.py:41 accepts an expired token. Source not edited.'
qa_comment NEEDS_HUMAN "$QA_HUMAN_FINDING"
QA_HUMAN="$STAGE_COMMENT"
grep -qF -- "$QA_HUMAN_FINDING" <<< "$QA_HUMAN" || fail "the QA NEEDS_HUMAN comment lacks the summary: $QA_HUMAN"
[ "${QA_HUMAN%%$'\n'*}" = "$V31_QA_HUMAN_HEADING" ] \
  || fail "the QA stage's NEEDS_HUMAN comment no longer starts with the v3.1.0 heading: ${QA_HUMAN%%$'\n'*}"
grep -q $'^add_issue_label\t'"$ISSUE"$'\tneeds-human$' "$SANDBOX/calls.log" || fail 'QA NEEDS_HUMAN did not label needs-human'
echo 'PASS the QA stage posted a RED and a NEEDS_HUMAN comment with their summaries'

# ── the implement stage reads them ─────────────────────────────────────────
MARKER='<!-- bureau-branch: test-branch -->
**Spec Artifacts — EXP-806**'
HALT='🐑 Shepherd halt: `needs-human` label present at `Build Review`. The stage that just ran flagged this ticket for human review; shepherd will not re-run it. Remove the label and re-shepherd when ready.'
OPERATOR='FIXES_NEEDED — the operator'"'"'s list for the next build pass

1. Validate the token in the request handler; the second finding of the review is overruled.'
QUOTE="Removed needs-human after these comments:
$V31_BLOCK_HEADING
$V31_QA_RED_HEADING
$V31_QA_HUMAN_HEADING
Restarting from build; the findings above stand."
SPEC_PASS='✅ Spec review **PASSED**. Ready for implementation.

The artifacts match the repo.'
QA_PASS='✅ QA **PASSED**. Moving to Build Review.

All tests pass.'
LABEL='--- Feedback to address (PRIORITY) ---'

# implement_prompt <comment>... — the REAL implement stage on a ticket with the branch marker
# and these comments, oldest first, a minute apart; sets PROMPT to what the agent was told and
# FEEDBACK to its feedback block (whatever its label). With BOUNCE=1 every task is already
# checked on the branch and the agent reports COMPLETE without a commit, as after a QA bounce.
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
  export FAKE_CLAUDE_PROMPT_LOG="$SANDBOX/prompts.log" BUREAU_IMPL_MAX_ITER=1 BUREAU_STUB_ISSUE_STATE=Build
  if [ "${BOUNCE:-0}" = 1 ]; then
    sed -i.bak 's/^- \[ \]/- [X]/' "$SANDBOX/specs/001-test-branch/tasks.md" && rm -f "$SANDBOX/specs/001-test-branch/tasks.md.bak"
    git -C "$SANDBOX" add specs && git -C "$SANDBOX" commit -q -m "$ISSUE: tests — expired token" && git -C "$SANDBOX" push -q origin test-branch
    sed 's/"tasks_done": 3/"tasks_done": 0/' "$FIXTURES_DIR/claude_complete.txt" > "$SANDBOX/complete-nothing-left.txt"
    export FAKE_CLAUDE_FIXTURES="$SANDBOX/complete-nothing-left.txt" BUREAU_STUB_STATE_QA=state-qa
    unset FAKE_CLAUDE_COMMIT_ON_ITERS
  else
    export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS=1
  fi
  run_implement_pipeline "$ISSUE"
  unset BUREAU_STUB_STATE_QA
  [ "$LAST_RC" = 0 ] || fail "the implement stage ended $LAST_RC"
  grep -q '^linear_issue_query' "$SANDBOX/calls.log" || fail 'the implement stage never read the comments through the double'
  PROMPT=$(cat "$SANDBOX/prompts.log" 2>/dev/null || true)
  [ -n "$PROMPT" ] || fail 'the implement stage never called the agent'
  # The feedback block alone, so a check cannot be fooled by text elsewhere in the prompt.
  FEEDBACK=$(awk '/^--- .*\(PRIORITY\) ---$/ {on=1; next} $0 == "--- End feedback ---" {on=0} on' <<< "$PROMPT")
}

has() { grep -qF -- "$1" <<< "$FEEDBACK"; }
shown() { printf '%s' "${FEEDBACK:0:300}"; }

# a. a BLOCK, then the shepherd's halt comment
implement_prompt "$SPEC_PASS" "$BLOCK" "$HALT"
if has "$BLOCK_FINDING" && has "$V31_BLOCK_HEADING"; then
  echo 'PASS a: after a BLOCK the prompt carries the BLOCK and its findings'
else
  check_fail "a: the prompt carries no BLOCK findings; feedback block: [$(shown)]" a
fi

# b. a BLOCK, then the operator's FIXES_NEEDED
implement_prompt "$SPEC_PASS" "$BLOCK" "$HALT" "$OPERATOR"
if has "the operator's list for the next build pass" && ! has "$BLOCK_FINDING"; then
  echo 'PASS b: an operator FIXES_NEEDED written after the BLOCK is what the prompt carries'
else
  check_fail "b: wanted the operator's FIXES_NEEDED alone; feedback block: [$(shown)]" b
fi

# c. an older Changes Requested, then a newer BLOCK
implement_prompt "$SPEC_PASS" "$CHANGES" "$BLOCK" "$HALT"
if has "$BLOCK_FINDING" && ! has "$RC_FINDING"; then
  echo 'PASS c: a BLOCK newer than a Changes Requested is what the prompt carries'
else
  check_fail "c: wanted the newer BLOCK alone; feedback block: [$(shown)]" c
fi

# d. no review or QA comment
implement_prompt "$SPEC_PASS" '🎨 Design phase complete. Ready for implementation.'
if ! grep -qF -- '(PRIORITY) ---' <<< "$PROMPT"; then
  echo 'PASS d: without a review or QA comment the prompt has no feedback block'
else
  check_fail "d: a feedback block without a review or QA comment: [$(shown)]" d
fi

# e. a BLOCK, then a note that quotes the three headings on lines of their own
implement_prompt "$SPEC_PASS" "$BLOCK" "$HALT" "$QUOTE"
if has "$BLOCK_FINDING" && ! has 'Restarting from build; the findings above stand.'; then
  echo 'PASS e: a note quoting the headings is not taken for a BLOCK or a QA comment'
else
  check_fail "e: wanted the BLOCK, not the note quoting the headings; feedback block: [$(shown)]" e
fi

# f. QA RED
implement_prompt "$SPEC_PASS" "$QA_RED"
if has "$QA_RED_FINDING" && has "$V31_QA_RED_HEADING" && grep -qxF -- "$LABEL" <<< "$PROMPT" \
    && ! grep -qF 'Code Review Feedback' <<< "$PROMPT" && has 'Address ALL fixes before remaining tasks.'; then
  echo 'PASS f: after QA RED the prompt carries QA'"'"'s summary under a label that names no code review'
else
  check_fail "f: wanted QA's summary under '$LABEL'; feedback block: [$(shown)]" f
fi

# g. QA RED, then QA PASSED and a newer Changes Requested
implement_prompt "$SPEC_PASS" "$QA_RED" "$QA_PASS" "$CHANGES"
if has "$RC_FINDING" && ! has "$QA_RED_FINDING"; then
  echo 'PASS g: a Changes Requested newer than QA RED is what the prompt carries'
else
  check_fail "g: wanted the newer review alone; feedback block: [$(shown)]" g
fi

# h. a Changes Requested, then a newer QA RED (the round after the review's rework)
implement_prompt "$SPEC_PASS" "$CHANGES" "$QA_RED"
if has "$QA_RED_FINDING" && ! has "$RC_FINDING"; then
  echo 'PASS h: a QA RED newer than a Changes Requested is what the prompt carries'
else
  check_fail "h: wanted the newer QA RED alone; feedback block: [$(shown)]" h
fi

# i. QA RED, then the operator's FIXES_NEEDED
implement_prompt "$SPEC_PASS" "$QA_RED" "$OPERATOR"
if has "the operator's list for the next build pass" && ! has "$QA_RED_FINDING"; then
  echo 'PASS i: an operator FIXES_NEEDED written after QA RED is what the prompt carries'
else
  check_fail "i: wanted the operator's FIXES_NEEDED alone; feedback block: [$(shown)]" i
fi

# j. QA NEEDS_HUMAN, then the shepherd's halt comment
implement_prompt "$SPEC_PASS" "$QA_HUMAN" "$HALT"
if has "$QA_HUMAN_FINDING" && has "$V31_QA_HUMAN_HEADING"; then
  echo 'PASS j: after QA NEEDS_HUMAN the prompt carries QA'"'"'s summary'
else
  check_fail "j: the prompt carries no QA NEEDS_HUMAN summary; feedback block: [$(shown)]" j
fi

# k. the QA bounce: every task checked by QA, then RED; the agent finds nothing left to do
BOUNCE=1 implement_prompt "$SPEC_PASS" "$QA_RED"
if grep -q 'iter 1: status=COMPLETE tasks_done=0 commits=0' <<< "$LAST_STDOUT" \
    && grep -q $'^move_issue\t'"$ISSUE"$'\tstate-qa$' "$SANDBOX/calls.log" \
    && ! grep -q 'needs-human' "$SANDBOX/calls.log" && ! grep -q 'status=STUCK' <<< "$LAST_STDOUT"; then
  echo 'PASS k-handoff: after a QA bounce a COMPLETE without a commit still goes on to QA, as before'
else
  check_fail "k-handoff: the bounce no longer hands on to QA; stdout tail: [$(printf '%s' "$LAST_STDOUT" | tail -5)]" k-handoff
fi
if has "$QA_RED_FINDING"; then
  echo 'PASS k-prompt: the bounced pass sees QA'"'"'s summary'
else
  check_fail "k-prompt: the bounced pass got no QA summary; feedback block: [$(shown)]" k-prompt
fi

[ -z "$FAILED" ] || { echo "FAILED cases:$FAILED" >&2; exit 1; }
echo 'OK test_implement_rework_feedback'
