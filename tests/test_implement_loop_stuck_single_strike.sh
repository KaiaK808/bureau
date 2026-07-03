#!/bin/bash
# STUCK: one iter, status=PARTIAL but tasks_done=0, no commits, no review fixes.
# Single-strike rule fires → STATUS=STUCK, needs-human labeled, no state move.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-102" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_no_progress.txt"
# No FAKE_CLAUDE_COMMIT_ON_ITERS → no commit produced → stuck detector fires.
export BUREAU_DRY_RUN=0
export BUREAU_IMPL_MAX_ITER=3

run_implement_pipeline

assert_eq 0 "$LAST_RC" "exit code (stuck still exits 0)"
assert_match 'iter 1: status=PARTIAL tasks_done=0 commits=0' "$LAST_STDOUT" "no-progress iter logged"
assert_match 'No work evidence this iteration'                "$LAST_STDOUT" "stuck-detector message"
assert_match 'status=STUCK'                                   "$LAST_STDOUT" "terminal STUCK"

# Linear side-effects: needs-human labeled, NO state move.
assert_calls_include 'add_issue_label.*needs-human'  "needs-human labeled"
assert_calls_exclude '^move_issue.*state-build-review' "no state move on stuck"
assert_calls_exclude '^move_issue.*state-qa'           "no state move on stuck"

# Escalation log: one TSV line written by log_escalation hook.
assert_file_contains 'ESCALATED.*EXP-102.*implement.*reason="STUCK' \
  "$SANDBOX/logs/escalations.log" "escalation line written"
assert_calls_include '^log_escalation.*EXP-102.*implement' "log_escalation called"

echo "OK test_implement_loop_stuck_single_strike"
