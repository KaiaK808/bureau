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
#  20b false, PR open, the stage's terminal hangs up while the hook runs (set -e is on there):
#      every write to stderr fails, and the EXIT trap still pushes the held-back commits
#  20c the same with stderr a pipe whose reader is gone (the queue loop's tee after a hang-up)
#      and SIGTERM to the stage, as its runtime forwards it: SIGPIPE does not end the trap
#      before the push
#  13  negative control: the stage with the per-iteration and /goal pushes of v3.0.2 pushes
#      after every iteration under false with a PR open
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr3-doubles.sh"

FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
check_eq() { [ "$1" = "$2" ] || fail "$3: expected '$1', got '$2'"; }
has() { grep -qE -- "$1" <<< "$2" || fail "$3 (no match for /$1/)"; }
hasnt() { if grep -qE -- "$1" <<< "$2"; then fail "$3 (unexpected /$1/)"; fi; }
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

# 20b, 20c — the deferred push after a hang-up, when stderr is gone. Iteration 1's push is held
# back (false, PR open); right after call 2 the stage is stopped while it waits for git under
# set -e, as it runs almost everywhere outside the provider calls (the hook runs inside a
# redirected call, where stderr is its log, so it cannot show this). The stage's EXIT trap then
# has the held-back commits to push and a dead stderr to announce it on.
hangup_setup() {
  setup "$OFF"
  pr3_fake_claude
  pr3_ignore_harness_files
  export GH_STUB_EXISTING_PR=7 PR3_ON_CALL_2="touch .pr3-hold-git"
  # The first `git rev-parse` after call 2 (HEAD_AFTER) marks that the stage waits there and
  # holds it until the stage has been hung up or signalled; every other git call passes through.
  mv "$SANDBOX/.pr3-bin/git" "$SANDBOX/.pr3-bin/git-counted"
  cat > "$SANDBOX/.pr3-bin/git" <<SHIM
#!/bin/bash
if [ "\${1:-}" = rev-parse ] && [ -f "$SANDBOX/.pr3-hold-git" ]; then
  rm -f "$SANDBOX/.pr3-hold-git"; : > "$SANDBOX/.pr3-stage-waits"
  i=0; while [ ! -f "$SANDBOX/.pr3-stage-stopped" ] && [ "\$i" -lt 300 ]; do sleep 0.1; i=\$((i + 1)); done
fi
exec "$SANDBOX/.pr3-bin/git-counted" "\$@"
SHIM
  chmod +x "$SANDBOX/.pr3-bin/git"
}
# hangup_done — wait (up to 10 s) until no process of the sandbox runs any more. Lookups only;
# nothing is signalled here.
hangup_done() {
  local i=0
  : > "$SANDBOX/.pr3-stage-stopped"
  unset PR3_ON_CALL_2
  while ps -A -o args= | grep -F "$SANDBOX/" | grep -v grep >/dev/null && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
}

# 20b — the terminal closes: the kernel hangs up the stage, and every write to it fails
hangup_setup
result=$(cd "$SANDBOX" && PATH="$SANDBOX/.pr3-bin:$PATH" python3 "$LIB_DIR/hangup.py" --ready "$SANDBOX/.pr3-stage-waits" \
  --how close --hung-up "$SANDBOX/.pr3-stage-stopped" --out "$SANDBOX/stdout.log" -- bash "$SCRIPTS_DIR/implement-pipeline.sh") || true
hangup_done
check_eq 129 "$(sed -n 's/^rc=\([0-9]*\) .*/\1/p' <<< "$result")" "20b ended by the hang-up ($result)"
has 'push deferred \(iter 1\)' "$(cat "$SANDBOX/stdout.log")" "20b iteration 1's push was held back"
check_eq 1 "$(pr3_pushes)" "20b one push, from the EXIT trap"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "20b every commit of the run on origin"
teardown

# 20c — stderr is a pipe whose reader is gone, and the stage gets SIGTERM: bash 3.2 dies of
# SIGPIPE at the first write there, with or without set -e
hangup_setup
mkfifo "$SANDBOX/.pr3-stderr"
cat "$SANDBOX/.pr3-stderr" > "$SANDBOX/stderr.log" &
READER=$!
(cd "$SANDBOX" && PATH="$SANDBOX/.pr3-bin:$PATH" exec bash "$SCRIPTS_DIR/implement-pipeline.sh" > "$SANDBOX/stdout.log" 2> "$SANDBOX/.pr3-stderr") &
STAGE=$!
waited=0
while [ ! -f "$SANDBOX/.pr3-stage-waits" ] && [ "$waited" -lt 300 ]; do sleep 0.1; waited=$((waited + 1)); done
[ -f "$SANDBOX/.pr3-stage-waits" ] || fail "20c the stage never reached the held git call"
kill "$READER" 2>/dev/null || true
wait "$READER" 2>/dev/null || true
kill -TERM "$STAGE" 2>/dev/null || true
: > "$SANDBOX/.pr3-stage-stopped"
{ wait "$STAGE"; rc=$?; } 2>/dev/null || true
hangup_done
check_eq 143 "$rc" "20c ended by SIGTERM"
check_eq 1 "$(pr3_pushes)" "20c one push, from the EXIT trap"
check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "20c every commit of the run on origin"
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
