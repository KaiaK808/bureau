#!/bin/bash
# /goal-driven PARTIAL: the single $CLAUDE invocation returns PARTIAL with
# a real commit (productive-but-cap-time-ish). EXP-622 ready-flip applies on
# the /goal path the same as the iter-loop path: PARTIAL+commits>0 → PR ready,
# needs-human label, stays in Build (no state move). Mirrors the contract
# from test_implement_loop_cap_partial.sh but on BUREAU_USE_GOAL_LOOP=1.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-103" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt"
export FAKE_CLAUDE_COMMIT_ON_ITERS="1"
export BUREAU_DRY_RUN=0
export BUREAU_IMPL_MAX_ITER=3

export BUREAU_USE_GOAL_LOOP=1

run_implement_pipeline

assert_eq 0 "$LAST_RC" "exit code (PARTIAL still exits 0 with the goal path)"
assert_match '/goal-driven implementation' "$LAST_STDOUT" "goal-path banner printed"
assert_match '/goal: status=PARTIAL tasks_done=1 commits=1' "$LAST_STDOUT" "goal-line breadcrumb"
assert_match 'status=PARTIAL' "$LAST_STDOUT" "terminal PARTIAL"

# PARTIAL with real commits → needs-human label + stays in Build. No state move.
assert_calls_include 'add_issue_label.*needs-human' "needs-human labeled"
assert_calls_exclude '^move_issue.*state-build-review' "no state move on PARTIAL"
assert_calls_exclude '^move_issue.*state-qa' "no state move on PARTIAL"

# Iter-loop output must NOT appear — same negative as the COMPLETE sibling.
if grep -qE 'iter [0-9]+:|No work evidence this iteration' <<< "$LAST_STDOUT"; then
  echo "FAIL: iter-loop output leaked into the /goal path"
  printf '%s\n' "$LAST_STDOUT" | sed 's/^/  | /'
  exit 1
fi

echo "OK test_implement_loop_partial_goal"
