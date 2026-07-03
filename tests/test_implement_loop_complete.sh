#!/bin/bash
# COMPLETE path: one iter, status=COMPLETE → state move, no needs-human.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-100" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
# Fixture self-reports tasks_done=3, but implement-pipeline's belt-and-suspenders
# check (EXP-573) overrides terminal status=COMPLETE → STUCK when COMMITS_TOTAL==0.
# Produce a real commit on iter 1 so the COMPLETE path survives the override.
export FAKE_CLAUDE_COMMIT_ON_ITERS="1"
export BUREAU_DRY_RUN=0
export BUREAU_IMPL_MAX_ITER=3

run_implement_pipeline

assert_eq 0 "$LAST_RC" "exit code"
assert_match 'status=COMPLETE' "$LAST_STDOUT" "stdout reports COMPLETE"
assert_match 'iter 1: status=COMPLETE tasks_done=3 commits=1' "$LAST_STDOUT" "iteration breadcrumb"

# Linear side-effects: move_issue called with Build Review state (no QA configured),
# needs-human label NOT added.
assert_calls_include '^move_issue.*state-build-review$' "moved to Build Review"
assert_calls_exclude 'add_issue_label.*needs-human' "no needs-human label"

# Comment posted with COMPLETE header.
assert_calls_include 'post_comment.*Implementation complete' "complete comment posted"

echo "OK test_implement_loop_complete"
