#!/bin/bash
# The review log says when it shows only the end of the build output (v3.1.0-rc.2).
#
# The review stage prints the last 20 lines of its build check. For a cargo workspace those
# are the doc-test summaries ("running 0 tests", four times), and the log read as if no test
# had run (pilot EXP-1545). A longer output is now introduced with "Build output: last 20 of
# <n> lines"; an output of 20 lines or fewer is printed whole, without the line.
#
# Runs the REAL code-review-pipeline.sh in the harness sandbox (stub gh, stub Linear, fake
# reviewers), with repo.test_command printing numbered lines.
#   1. 30 lines: the line names 20 of 30, and exactly lines 11 to 30 follow
#   2. 5 lines: printed whole, no line
# Negative control: against v3.1.0-rc.1 (5184cf8) case 1 has no such line and fails.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP
trap 'teardown || true' EXIT
fail() { echo "FAIL $*" >&2; printf '%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }

# run_review <lines> — a review whose build check prints "build line 1" … "build line <lines>".
run_review() {
  teardown || true
  sandbox_init EXP-704 test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  jq -n --arg c "i=1; while [ \$i -le $1 ]; do echo \"build line \$i\"; i=\$((i + 1)); done" '{repo: {test_command: $c}}' > "$SANDBOX/.bureau.json"
  printf 'Review checked.\n```json\n{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"fixture"}\n```\n' > "$SANDBOX/verdict.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/verdict.txt"
  export BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  export BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=0
  run_pipeline code-review-pipeline.sh EXP-704
  rm -rf "$(printf '%s\n' "$LAST_STDERR" | sed -n 's/^code-review failed .*preserved at //p')"
  SHOWN=$(sed -n '/^  Running build check: /,/^  Build passed$/p' <<< "$LAST_STDOUT" | grep '^build line ' || true)
}

run_review 30
[ "$LAST_RC" = 20 ] || fail "1: the review ended $LAST_RC, wanted 20 (stop before merge)"
grep -qx '  Build output: last 20 of 30 lines' <<< "$LAST_STDOUT" || fail "1: no line saying 20 of 30 lines are shown"
[ "$SHOWN" = "$(i=11; while [ $i -le 30 ]; do echo "build line $i"; i=$((i + 1)); done)" ] || fail "1: not exactly lines 11 to 30 shown: $SHOWN"
echo "PASS 1 a long build output is introduced with how much of it is shown"

run_review 5
grep -q 'Build output: last 20 of' <<< "$LAST_STDOUT" && fail "2: a 5-line output is introduced as cut"
[ "$(grep -c . <<< "$SHOWN")" = 5 ] || fail "2: the 5-line output is not shown whole: $SHOWN"
echo "PASS 2 a short build output is printed whole, without the line"
