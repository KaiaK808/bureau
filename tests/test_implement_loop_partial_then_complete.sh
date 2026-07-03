#!/bin/bash
# PARTIAL → COMPLETE: two iters, first reports PARTIAL with progress (commit),
# second reports COMPLETE. Final state move happens; no needs-human label.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-101" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt:$FIXTURES_DIR/claude_complete.txt"
export FAKE_CLAUDE_COMMIT_ON_ITERS="1"   # iter 1 produces a real commit so stuck-detect doesn't fire
export BUREAU_DRY_RUN=0
export BUREAU_IMPL_MAX_ITER=3

run_implement_pipeline

assert_eq 0 "$LAST_RC" "exit code"
assert_match 'iter 1: status=PARTIAL tasks_done=1 commits=1' "$LAST_STDOUT" "iter 1 partial-with-progress"
assert_match 'iter 2: status=COMPLETE tasks_done=3'           "$LAST_STDOUT" "iter 2 complete"
assert_match 'status=COMPLETE'                                 "$LAST_STDOUT" "terminal COMPLETE"

assert_calls_include '^move_issue.*state-build-review$' "moved to Build Review"
assert_calls_exclude 'add_issue_label.*needs-human'      "no needs-human label"

echo "OK test_implement_loop_partial_then_complete"
