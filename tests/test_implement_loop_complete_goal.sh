#!/bin/bash
# /goal-driven COMPLETE: a single $CLAUDE -p "/goal ..." call returns the
# fenced JSON status block with status=COMPLETE+tasks_done=3, the harness's
# fake claude makes a real commit, and the pipeline moves the ticket to
# Build Review. Mirrors test_implement_loop_complete.sh but on the
# BUREAU_USE_GOAL_LOOP=1 code path.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-100" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
# Single /goal invocation = "iter 1" from the fake-claude harness's perspective.
# The fake commits on that call so the post-/goal BRANCH_COMMITS_AHEAD backstop
# doesn't flip the legit COMPLETE to STUCK.
export FAKE_CLAUDE_COMMIT_ON_ITERS="1"
export BUREAU_DRY_RUN=0
export BUREAU_IMPL_MAX_ITER=3

# Activate the /goal path. The stub's use_goal_loop_enabled checks this env.
export BUREAU_USE_GOAL_LOOP=1

run_implement_pipeline

assert_eq 0 "$LAST_RC" "exit code"
assert_match '/goal-driven implementation' "$LAST_STDOUT" "goal-path banner printed"
assert_match '/goal: status=COMPLETE tasks_done=3 commits=1' "$LAST_STDOUT" "goal-line breadcrumb"
assert_match 'status=COMPLETE' "$LAST_STDOUT" "terminal COMPLETE"

# Linear side-effects: move to Build Review, no needs-human, complete comment.
assert_calls_include '^move_issue.*state-build-review$' "moved to Build Review"
assert_calls_exclude 'add_issue_label.*needs-human' "no needs-human label"
assert_calls_include 'post_comment.*Implementation complete' "complete comment posted"

# The iter-loop path is NOT exercised on this run — neither the per-iter
# breadcrumb nor the stuck-detector message should appear.
if grep -qE 'iter [0-9]+:|No work evidence this iteration' <<< "$LAST_STDOUT"; then
  echo "FAIL: iter-loop output leaked into the /goal path"
  printf '%s\n' "$LAST_STDOUT" | sed 's/^/  | /'
  exit 1
fi

echo "OK test_implement_loop_complete_goal"
