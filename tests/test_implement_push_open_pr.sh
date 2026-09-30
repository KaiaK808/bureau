#!/bin/bash
# agents.implement.push_each_iteration on the real implement stage (tests/lib/harness.sh:
# stubbed Linear, gh and model; real git with a bare origin). Every `git push` the stage
# makes is counted by a pass-through in front of the real git, every push that reached
# origin by a post-receive hook in the bare origin.
#
#   1  default (key absent), PR open: a push after each of three iterations plus the one at
#      the end; the open-PR state is not even asked
#   2  false, PR open: no push after an iteration, one at the end; every commit on origin;
#      the PR state asked exactly once; the push comes before the PR is marked ready
#   3  false, no PR: a push after each iteration, as by default
#   4  false, gh prints the literal "null": no PR, pushes as by default
#   5  false, gh fails: no PR, pushes as by default, the stage goes on
#   6  null, PR open: the default
#   7  `agents.implement: true` (a boolean), PR open: the default, without a warning
#   8  the string "false", PR open: the default, with a warning naming the value
#   9  /goal path, false, PR open: no push after the run, one at the end, hand-off
#  10  /goal path, default, PR open: the push after the run and the one at the end
#  11  false, PR open, origin rejects every push: the end-of-run push is retried once, then
#      18 before any hand-off (the final-push rules are unchanged)
#  12  false, PR open, COMPLETE: one push, then the PR is marked ready and the ticket moves
#  4b  false, gh answers text that is not a number (exit 0): no PR, pushes as by default
#  14  false, PR open, the provider times out (124) on call 3: the held-back iterations are
#      pushed before the stage ends, and it still ends with 124
#  15  the same with an exhausted quota (23) on a call that made no commit
#  16  the same with an interrupted provider (130)
#  17  the same when the stage itself gets SIGTERM during call 2 (the EXIT trap): 143
#  18  default, provider timeout on call 3: the iterations before it are on origin from their
#      own pushes, and nothing extra is pushed
#  19  false, PR open, a hook that resets HEAD to origin's tip: 14, HEAD is not pushed, the
#      commit the hook started from is
#  20  false, PR open, SIGTERM while a hook that reset HEAD runs: the EXIT trap pushes the
#      commit the hook started from
#  13  negative control: the stage with the per-iteration and /goal pushes of v3.0.2 pushes
#      after every iteration under false with a PR open
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr3-doubles.sh"

FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
check_eq() { [ "$1" = "$2" ] || fail "$3: expected '$1', got '$2'"; }
has() { printf '%s' "$2" | grep -qE -- "$1" || fail "$3 (no match for /$1/)"; }
hasnt() { if printf '%s' "$2" | grep -qE -- "$1"; then fail "$3 (unexpected /$1/)"; fi; }
calls() { cat "$SANDBOX/calls.log" 2>/dev/null || true; }
gh_log() { cat "$SANDBOX/gh_calls.log" 2>/dev/null || true; }
open_pr_reads() { grep -c $'^gh\tpr\tlist\t--head\ttest-branch\t--state\topen' "$SANDBOX/gh_calls.log" 2>/dev/null || true; }
origin_tip() { git -C "$SANDBOX/.fake-origin.git" rev-parse test-branch; }

setup() {  # setup [implement-config-json] — three iterations, each with a commit, PARTIAL
  sandbox_init "EXP-100" "test-branch"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt"
  export FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3" BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
  unset BUREAU_USE_GOAL_LOOP FAKE_CLAUDE_COMMIT_MSG GH_STUB_EXISTING_PR
  if [ -n "${1:-}" ]; then
    jq -n --argjson impl "$1" '{agents: {implement: $impl}}' > "$SANDBOX/.bureau.json"
  fi
  pr3_count_pushes
  pr3_count_receives
}
OFF='{"enabled": true, "push_each_iteration": false}'

# 1 — default, PR open
setup
export GH_STUB_EXISTING_PR=7
pr3_run_implement
check_eq 0 "$LAST_RC" "1 exit"
has 'terminal status=PARTIAL' "$LAST_STDOUT" "1 PARTIAL"
check_eq 4 "$(pr3_pushes)" "1 three iteration pushes and the end-of-run push"
check_eq 3 "$(pr3_receives)" "1 origin received each iteration"
check_eq 0 "$(open_pr_reads)" "1 the open-PR state is not asked by default"
hasnt 'deferred' "$LAST_STDOUT" "1 nothing deferred"
teardown

# 2 — false, PR open
setup "$OFF"
export GH_STUB_EXISTING_PR=7
pr3_run_implement
check_eq 0 "$LAST_RC" "2 exit"
has 'terminal status=PARTIAL' "$LAST_STDOUT" "2 PARTIAL"
check_eq 1 "$(pr3_pushes)" "2 one push, at the end"
check_eq 1 "$(pr3_receives)" "2 origin received once"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "2 every commit on origin"
check_eq 1 "$(open_pr_reads)" "2 the open-PR state asked exactly once"
has 'PR #7 is open for test-branch and push_each_iteration is false' "$LAST_STDOUT" "2 says why"
check_eq 3 "$(printf '%s\n' "$LAST_STDOUT" | grep -c 'push deferred (iter [123])' || true)" "2 three deferred pushes"
order=$(gh_log | grep -nE $'^(git\tpush|gh\tpr\tready)' | cut -d: -f2 | cut -f1-2 | tr '\t\n' ' |')
check_eq 'git push|gh pr|' "$order" "2 the push comes before the PR is marked ready"
teardown

# 3 — false, no PR
setup "$OFF"
pr3_run_implement
check_eq 4 "$(pr3_pushes)" "3 pushes as by default"
check_eq 3 "$(pr3_receives)" "3 origin received each iteration"
check_eq 1 "$(open_pr_reads)" "3 asked once"
teardown

# 4 — false, gh answers the literal "null"
setup "$OFF"
export GH_STUB_EXISTING_PR=7   # the null answer below wins for `gh pr list`
pr3_gh_pr_list null
pr3_run_implement
check_eq 4 "$(pr3_pushes)" "4 null is no PR"
check_eq 3 "$(pr3_receives)" "4 origin received each iteration"
teardown

# 4b — false, gh answers something that is not a PR number
setup "$OFF"
export GH_STUB_EXISTING_PR=7
pr3_gh_pr_list junk
pr3_run_implement
check_eq 4 "$(pr3_pushes)" "4b a non-numeric answer is no PR"
teardown

# 5 — false, gh fails
setup "$OFF"
export GH_STUB_EXISTING_PR=7
pr3_gh_pr_list fail
pr3_run_implement
check_eq 0 "$LAST_RC" "5 exit"
check_eq 4 "$(pr3_pushes)" "5 a failing gh is no PR"
check_eq 3 "$(pr3_receives)" "5 origin received each iteration"
teardown

# 6 — null
setup '{"enabled": true, "push_each_iteration": null}'
export GH_STUB_EXISTING_PR=7
pr3_run_implement
check_eq 4 "$(pr3_pushes)" "6 null is the default"
hasnt 'WARN: .agents.implement.push_each_iteration' "$LAST_STDERR" "6 no warning"
teardown

# 7 — agents.implement: true
setup 'true'
export GH_STUB_EXISTING_PR=7
pr3_run_implement
check_eq 4 "$(pr3_pushes)" "7 a boolean agents.implement is the default"
hasnt 'WARN: .agents.implement.push_each_iteration' "$LAST_STDERR" "7 no warning"
teardown

# 8 — the string "false"
setup '{"enabled": true, "push_each_iteration": "false"}'
export GH_STUB_EXISTING_PR=7
pr3_run_implement
check_eq 4 "$(pr3_pushes)" "8 a string is the default"
has 'WARN: .agents.implement.push_each_iteration = "false" is not a JSON boolean' "$LAST_STDERR" "8 warning names the value"
check_eq 0 "$(open_pr_reads)" "8 not asked"
teardown

# 9 — /goal path, false, PR open
setup "$OFF"
export GH_STUB_EXISTING_PR=7 BUREAU_USE_GOAL_LOOP=1 FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="1"
pr3_run_implement
check_eq 0 "$LAST_RC" "9 exit"
has 'push deferred \(/goal run\)' "$LAST_STDOUT" "9 the push after the run deferred"
check_eq 1 "$(pr3_pushes)" "9 one push, at the end"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "9 the work on origin"
has 'move_issue.*state-build-review' "$(calls)" "9 hand-off"
teardown

# 10 — /goal path, default, PR open
setup
export GH_STUB_EXISTING_PR=7 BUREAU_USE_GOAL_LOOP=1 FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="1"
pr3_run_implement
check_eq 2 "$(pr3_pushes)" "10 the push after the run and the one at the end"
check_eq 1 "$(pr3_receives)" "10 origin received the run's commit once"
teardown

# 11 — false, PR open, origin rejects every push
setup "$OFF"
export GH_STUB_EXISTING_PR=7
printf '#!/bin/sh\necho "rejected by test" >&2\nexit 1\n' > "$SANDBOX/.fake-origin.git/hooks/pre-receive"
chmod +x "$SANDBOX/.fake-origin.git/hooks/pre-receive"
pr3_run_implement
check_eq 18 "$LAST_RC" "11 exit"
check_eq 2 "$(pr3_pushes)" "11 the end-of-run push and its retry"
has 'post_comment.*final push of `test-branch` to origin failed twice \(3 commit\(s\) missing on origin\)' "$(calls)" "11 comment names the missing commits"
hasnt 'move_issue' "$(calls)" "11 no hand-off"
hasnt $'^gh\tpr\tready' "$(gh_log)" "11 PR not marked ready"
teardown

# 12 — false, PR open, COMPLETE
setup "$OFF"
export GH_STUB_EXISTING_PR=7 FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="1"
pr3_run_implement
check_eq 0 "$LAST_RC" "12 exit"
check_eq 1 "$(pr3_pushes)" "12 one push"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "12 the work on origin"
order=$(gh_log | grep -nE $'^(git\tpush|gh\tpr\tready)' | cut -d: -f2 | cut -f1-2 | tr '\t\n' ' |')
check_eq 'git push|gh pr|' "$order" "12 the push comes before the PR is marked ready"
has 'move_issue.*state-build-review' "$(calls)" "12 hand-off"
teardown

# 14–16 — false, PR open, the provider fails: the held-back commits go out before the exit
for c in "14 3 124" "15 2 23" "16 2 130"; do
  set -- $c
  setup "$OFF"
  pr3_fake_claude
  export GH_STUB_EXISTING_PR=7 PR3_EXIT_ON="$2" PR3_EXIT_CODE="$3"
  [ "$1" != 15 ] || export FAKE_CLAUDE_COMMIT_ON_ITERS="1"
  pr3_run_implement
  unset PR3_EXIT_ON PR3_EXIT_CODE
  check_eq "$3" "$LAST_RC" "$1 the provider's exit code is kept"
  check_eq 1 "$(pr3_pushes)" "$1 one push, before the exit"
  check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "$1 every commit of the run on origin"
  has "pushing the deferred commits of test-branch \(provider exit $3\)" "$LAST_STDERR" "$1 says why"
  hasnt 'move_issue' "$(calls)" "$1 no hand-off"
  teardown
done

# 17 — false, PR open, the stage gets SIGTERM during call 2
setup "$OFF"
pr3_fake_claude
export GH_STUB_EXISTING_PR=7 PR3_TERM_STAGE_ON=2
pr3_run_implement
unset PR3_TERM_STAGE_ON
check_eq 143 "$LAST_RC" "17 ended by SIGTERM"
check_eq 1 "$(pr3_pushes)" "17 one push, from the EXIT trap"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "17 every commit of the run on origin"
has 'pushing the deferred commits of test-branch \(the stage ends before its end-of-run push\)' "$LAST_STDERR" "17 says why"
teardown

# 18 — default, provider timeout on call 3: nothing extra
setup
pr3_fake_claude
export GH_STUB_EXISTING_PR=7 PR3_EXIT_ON=3 PR3_EXIT_CODE=124
pr3_run_implement
unset PR3_EXIT_ON PR3_EXIT_CODE
check_eq 124 "$LAST_RC" "18 exit"
check_eq 2 "$(pr3_pushes)" "18 the two iteration pushes only"
hasnt 'deferred' "$LAST_STDERR$LAST_STDOUT" "18 nothing deferred"
teardown

# 19 — false, PR open, the hook resets HEAD to origin's tip
setup
jq -n --argjson impl "$OFF" '{agents: {implement: $impl}, repo: {post_implement_command: "git reset -q --hard origin/test-branch"}}' > "$SANDBOX/.bureau.json"
export GH_STUB_EXISTING_PR=7 FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="1"
pr3_run_implement
check_eq 14 "$LAST_RC" "19 exit"
has 'not pushing: repo.post_implement_command moved HEAD' "$LAST_STDERR" "19 HEAD not pushed"
check_eq 1 "$(pr3_pushes)" "19 one push"
check_eq 'fake-claude iter 1 progress' "$(git -C "$SANDBOX/.fake-origin.git" log -1 --format=%s test-branch)" "19 the run's commit is on origin"
teardown

# 20 — false, PR open, the stage gets SIGTERM while a hook that reset HEAD is still running:
# the EXIT trap pushes the commit the hook started from, never the reset HEAD
setup
jq -n --argjson impl "$OFF" '{agents: {implement: $impl}, repo: {post_implement_command: "git reset -q --hard origin/test-branch; ./.pr3-term-stage; sleep 3"}}' > "$SANDBOX/.bureau.json"
pr3_term_stage_script
export GH_STUB_EXISTING_PR=7 FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="1"
pr3_run_implement
check_eq 143 "$LAST_RC" "20 ended by SIGTERM"
check_eq 'fake-claude iter 1 progress' "$(git -C "$SANDBOX/.fake-origin.git" log -1 --format=%s test-branch)" "20 the run's commit is on origin"
teardown

# 13 — negative control: the per-iteration and /goal pushes as in v3.0.2
setup "$OFF"
export GH_STUB_EXISTING_PR=7
f="$SCRIPTS_DIR/implement-pipeline.sh"
grep -q '^  push_iteration "iter \$i"$' "$f" && grep -q '^  push_iteration "/goal run"$' "$f" \
  || fail "13 control: the push lines were not found"
sed -i.bak -e 's/^  push_iteration "iter \$i"$/  push_branch_loud "iter $i"/' \
  -e 's|^  push_iteration "/goal run"$|  push_branch_loud "/goal run"|' "$f"
pr3_run_implement
check_eq 4 "$(pr3_pushes)" "13 control: the old stage pushes after every iteration under false"
teardown

trap - EXIT  # every case tore its own sandbox down
if [ "$FAILS" -gt 0 ]; then
  echo "test_implement_push_open_pr: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_implement_push_open_pr"
