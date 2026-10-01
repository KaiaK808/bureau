#!/bin/bash
# A provider pass that times out (exit 124) on the real implement stage (tests/lib/harness.sh:
# stubbed Linear, gh and model; real git with a bare origin). Since v3.2 a timed-out pass is
# counted like any pass and the loop goes on with another one, which is told why; only when
# no pass is left does the stage end with 124, as before. The model double ends a pass the
# way bureau-provider.py ends one that hit its limit: no output, exit 124.
#
#   1  T001 is done before the run; pass 1 marks the other two, commits and times out; pass 2
#      reports COMPLETE: the iteration log names the timed-out pass with its commit and its two
#      marks, only pass 2's prompt carries the note, the work reaches origin, the ticket moves
#      on, the summary shows both passes
#   2  pass 1 times out without a commit and with a mark taken back: not STUCK, tasks_done 0,
#      pass 2 runs and completes
#   3  every pass times out (BUREAU_IMPL_MAX_ITER=2): the stage ends with 124 after the last,
#      both passes are logged and pushed, nothing is handed on or labelled
#   4  a timeout that leaves too little of BUREAU_IMPL_TOTAL_TIMEOUT for another pass: 124
#      at once, not the CAP_TIME halt
#   5  another provider failure (22) still ends the stage after the pass, with its code
#   6  agents.implement.push_each_iteration false with a PR open: the timed-out pass's commit
#      is held back like any other and goes out with the end-of-run push
#   7  a Codex implementation: the note does not ask the agent to commit or to touch a git
#      lock (the shell commits after a Codex pass); case 1 checks the Claude wording
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr3-doubles.sh"

FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
check_eq() { [ "$1" = "$2" ] || fail "$3: expected '$1', got '$2'"; }
has() { printf '%s' "$2" | grep -qE -- "$1" || fail "$3 (no match for /$1/)"; }
hasnt() { if printf '%s' "$2" | grep -qE -- "$1"; then fail "$3 (unexpected /$1/)"; fi; }
calls() { cat "$SANDBOX/calls.log" 2>/dev/null || true; }
invocations() { cat "$SANDBOX/fake_claude_counter" 2>/dev/null || echo 0; }
origin_tip() { git -C "$SANDBOX/.fake-origin.git" rev-parse test-branch; }
NOTE='--- The previous pass timed out ---'

# mark_t001_done: T001 marked done in tasks.md, committed and on origin before the run.
mark_t001_done() {
  sed 's/^- \[ \] T001/- [X] T001/' "$SANDBOX/specs/001-test-branch/tasks.md" > "$SANDBOX/t.tmp"
  mv "$SANDBOX/t.tmp" "$SANDBOX/specs/001-test-branch/tasks.md"
  git -C "$SANDBOX" add specs && git -C "$SANDBOX" commit -q -m "T001 done earlier"
  git -C "$SANDBOX" push -q origin test-branch
}

setup() {
  sandbox_init "EXP-100" "test-branch"
  mkdir -p "$SANDBOX/.prompts"
  export FAKE_CLAUDE_PROMPT_DIR="$SANDBOX/.prompts" FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
  export BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
  unset FAKE_CLAUDE_COMMIT_ON_ITERS FAKE_CLAUDE_CHECK_TASKS_ON_ITERS FAKE_CLAUDE_TIMEOUT_ON_ITERS \
    FAKE_CLAUDE_TIMEOUT_SLEEP BUREAU_IMPL_TOTAL_TIMEOUT BUREAU_IMPL_ITER_TIMEOUT GH_STUB_EXISTING_PR \
    PR3_EXIT_ON PR3_EXIT_CODE PR3_ON_CALL_1 BUREAU_USE_GOAL_LOOP
}

# 1 — timeout after finished work, then COMPLETE
setup
mark_t001_done
export FAKE_CLAUDE_TIMEOUT_ON_ITERS=1 FAKE_CLAUDE_CHECK_TASKS_ON_ITERS=1 FAKE_CLAUDE_COMMIT_ON_ITERS=1
run_implement_pipeline
check_eq 0 "$LAST_RC" "1 exit"
check_eq 2 "$(invocations)" "1 a second pass ran"
has '^    iter 1: status=TIMEOUT tasks_done=2 commits=1 \(timed out after 1800s\)$' "$LAST_STDOUT" "1 the timed-out pass is logged with its two marks and its commit"
has 'iter 2: status=COMPLETE tasks_done=3 commits=0$' "$LAST_STDOUT" "1 pass 2 completes"
has 'terminal status=COMPLETE \(after 2 iter\(s\)\)' "$LAST_STDOUT" "1 terminal COMPLETE"
has 'iter 1 timed out after 1800s \(exit 124\); it counts as a pass' "$LAST_STDERR" "1 the timeout is said on stderr"
hasnt 'Provider pass failed' "$LAST_STDERR" "1 the stage did not end on the timeout"
if grep -qF -- "$NOTE" "$SANDBOX/.prompts/prompt-1.txt"; then fail "1 pass 1's prompt carries the note"; fi
grep -qF -- "$NOTE" "$SANDBOX/.prompts/prompt-2.txt" || fail "1 pass 2's prompt lacks the note"
grep -qF 'Pass 1 of this stage was stopped at its time limit of 1800s before it reported a status' "$SANDBOX/.prompts/prompt-2.txt" \
  || fail "1 the note names the pass and its limit"
grep -qF 'report status COMPLETE right away and stop' "$SANDBOX/.prompts/prompt-2.txt" || fail "1 the note asks for COMPLETE when nothing is left"
grep -qF 'commit finished work that is still uncommitted' "$SANDBOX/.prompts/prompt-2.txt" || fail "1 a Claude pass is asked to commit what is left"
grep -qF 'remove the lock only when no git process has this worktree as its working directory' "$SANDBOX/.prompts/prompt-2.txt" \
  || fail "1 the note removes a git lock only when no git process runs there"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "1 the timed-out pass's commit is on origin"
has 'move_issue	EXP-100	state-build-review' "$(calls)" "1 handed on to Build Review"
hasnt 'add_issue_label.*needs-human' "$(calls)" "1 no needs-human"
# The stub records a comment body over several lines of calls.log; the iteration log reaches
# calls.log only through the summary comment.
has 'iter 1: status=TIMEOUT tasks_done=2 commits=1 \(timed out after 1800s\)' "$(calls)" "1 the summary comment shows the timed-out pass"
teardown

# 2 — timeout without a commit, one mark taken back: not STUCK, the loop goes on
setup
mark_t001_done
pr3_fake_claude
export FAKE_CLAUDE_TIMEOUT_ON_ITERS=1
export PR3_ON_CALL_1="sed 's/^- \[X\] T001/- [ ] T001/' specs/001-test-branch/tasks.md > t.tmp && mv t.tmp specs/001-test-branch/tasks.md"
pr3_run_implement
check_eq 0 "$LAST_RC" "2 exit"
check_eq 2 "$(invocations)" "2 a second pass ran"
has 'iter 1: status=TIMEOUT tasks_done=0 commits=0 \(timed out after 1800s\)$' "$LAST_STDOUT" "2 no commit, no negative count"
hasnt 'STUCK' "$LAST_STDOUT" "2 a timed-out pass is not stuck"
has 'terminal status=COMPLETE \(after 2 iter\(s\)\)' "$LAST_STDOUT" "2 terminal COMPLETE"
teardown

# 3 — every pass times out
setup
export BUREAU_IMPL_MAX_ITER=2 FAKE_CLAUDE_TIMEOUT_ON_ITERS=1:2 FAKE_CLAUDE_COMMIT_ON_ITERS=1:2
run_implement_pipeline
check_eq 124 "$LAST_RC" "3 exit"
check_eq 2 "$(invocations)" "3 both passes ran"
has 'iter 1: status=TIMEOUT tasks_done=0 commits=1 \(timed out after 1800s\)$' "$LAST_STDOUT" "3 pass 1 logged"
has 'iter 2: status=TIMEOUT tasks_done=0 commits=1 \(timed out after 1800s\)$' "$LAST_STDOUT" "3 pass 2 logged"
has 'Provider pass failed with exit 124 \(iter 2 timed out\) and no pass is left: BUREAU_IMPL_MAX_ITER=2 passes have run' "$LAST_STDERR" "3 says why it ends"
grep -qF -- "$NOTE" "$SANDBOX/.prompts/prompt-2.txt" || fail "3 pass 2 was told"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "3 both passes' commits are on origin"
hasnt 'Phase 2/2' "$LAST_STDOUT" "3 no hand-off phase"
hasnt 'move_issue|add_issue_label|post_comment' "$(calls)" "3 nothing handed on, labelled or posted"
teardown

# 4 — the total budget leaves no room for another pass
setup
export BUREAU_IMPL_TOTAL_TIMEOUT=66 FAKE_CLAUDE_TIMEOUT_ON_ITERS=1 FAKE_CLAUDE_TIMEOUT_SLEEP=7 FAKE_CLAUDE_COMMIT_ON_ITERS=1
run_implement_pipeline
check_eq 124 "$LAST_RC" "4 exit"
check_eq 1 "$(invocations)" "4 no second pass"
has 'iter 1: status=TIMEOUT tasks_done=0 commits=1 \(timed out after 6[56]s\)$' "$LAST_STDOUT" "4 pass 1 logged with its share of the budget"
has 'no pass is left: the total budget BUREAU_IMPL_TOTAL_TIMEOUT=66s leaves [0-9]+s' "$LAST_STDERR" "4 says why it ends"
hasnt 'CAP_TIME|Total wall-time cap exhausted' "$LAST_STDOUT" "4 not the CAP_TIME halt"
hasnt 'move_issue|add_issue_label|post_comment' "$(calls)" "4 nothing handed on, labelled or posted"
teardown

# 5 — another provider failure still ends the stage at once
setup
pr3_fake_claude
export PR3_EXIT_ON=1 PR3_EXIT_CODE=22
pr3_run_implement
check_eq 22 "$LAST_RC" "5 exit"
check_eq 1 "$(invocations)" "5 no second pass"
has 'Provider pass failed with exit 22; preserved any changes' "$LAST_STDERR" "5 says so"
hasnt 'iter 1:' "$LAST_STDOUT" "5 no iteration bookkeeping"
teardown

# 6 — deferred pushes: the timed-out pass's commit waits for the end-of-run push
setup
jq -n '{agents: {implement: {enabled: true, push_each_iteration: false}}}' > "$SANDBOX/.bureau.json"
pr3_count_pushes
export GH_STUB_EXISTING_PR=7 FAKE_CLAUDE_TIMEOUT_ON_ITERS=1 FAKE_CLAUDE_COMMIT_ON_ITERS=1:2
pr3_run_implement
check_eq 0 "$LAST_RC" "6 exit"
has 'push deferred \(iter 1\)' "$LAST_STDOUT" "6 the timed-out pass's push is deferred"
check_eq 1 "$(pr3_pushes)" "6 one push, at the end"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "6 every commit on origin"
has 'terminal status=COMPLETE \(after 2 iter\(s\)\)' "$LAST_STDOUT" "6 terminal COMPLETE"
teardown

# 7 — Codex: the shell commits, the note leaves Git alone
setup
export BUREAU_STUB_RUNNER=codex BUREAU_IMPL_MAX_ITER=2 FAKE_CLAUDE_TIMEOUT_ON_ITERS=1:2 FAKE_CLAUDE_COMMIT_ON_ITERS=1
run_implement_pipeline
unset BUREAU_STUB_RUNNER
check_eq 124 "$LAST_RC" "7 exit"
grep -qF -- "$NOTE" "$SANDBOX/.prompts/prompt-2.txt" || fail "7 pass 2 was told"
grep -qF 'The Bureau shell committed the changes it left.' "$SANDBOX/.prompts/prompt-2.txt" || fail "7 the note says the shell committed"
if grep -qE 'commit finished work|index\.lock' "$SANDBOX/.prompts/prompt-2.txt"; then fail "7 a Codex pass is asked to commit or to touch a git lock"; fi
teardown

trap - EXIT  # every case tore its own sandbox down
if [ "$FAILS" -gt 0 ]; then
  echo "test_implement_timeout_pass: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_implement_timeout_pass"
