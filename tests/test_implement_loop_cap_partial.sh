#!/bin/bash
# CAP_PARTIAL: MAX_ITER=1, fixture is PARTIAL-with-progress. Loop exhausts the
# cap without reaching COMPLETE. STATUS normalises to PARTIAL → needs-human
# labeled, no state move.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-103" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt"
export FAKE_CLAUDE_COMMIT_ON_ITERS="1"
export BUREAU_DRY_RUN=0
export BUREAU_IMPL_MAX_ITER=1

run_implement_pipeline

assert_eq 0 "$LAST_RC" "exit code"
assert_match 'iter 1: status=PARTIAL tasks_done=1 commits=1' "$LAST_STDOUT" "single iter recorded"
assert_match 'status=PARTIAL'                                 "$LAST_STDOUT" "terminal PARTIAL"

assert_calls_include 'add_issue_label.*needs-human'     "needs-human labeled"
assert_calls_exclude '^move_issue.*state-build-review'  "no state move on cap-partial"

echo "OK test_implement_loop_cap_partial"
