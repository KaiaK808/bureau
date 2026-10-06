#!/bin/bash
# The push before implement ends on a failed provider pass, and the line that confirms a push
# (v3.2), on the real implement stage (tests/lib/harness.sh: stubbed Linear, gh and model; real
# git with a bare origin; every `git push` counted by the pass-through of pr3-doubles.sh). A
# pass that ends with any exit but 0 and 124, or a timed-out one with no pass left, pushes
# every commit origin lacks before the stage ends, whatever push_each_iteration says and also
# when it is the first pass: before, the deferred-push flag was set only after a pass that
# returned, so the commits of a first pass that died stayed in the worktree.
#
#   1  false, PR open: the first pass commits and the provider fails (1): the commit reaches
#      origin in one push, the stage still ends with 1, a line before and one after the push
#   2  the same with an interrupted provider (130)
#   3  the default (no push_each_iteration key), PR open: the same with 1 — the push does not
#      depend on the key
#   4  false, PR open, origin rejects every push: one attempt, loud, the provider's 1 is kept
#      (no 18), no confirmation line
#  4b  the pass also removed the remote-tracking ref, so the comparison with origin cannot be
#      read: that counts as ahead, the push goes out and its confirmation says so
#   5  guard: a pass that fails when origin already has every commit makes no push (the pass
#      before it was pushed, by default)
#   6  false, PR open, BUREAU_IMPL_MAX_ITER=1: the first and only pass commits and times out
#      (124): the commit reaches origin, the stage ends with 124
#   7  the same when BUREAU_IMPL_TOTAL_TIMEOUT leaves no room for a second pass
#  6b  the default, BUREAU_IMPL_MAX_ITER=1: the timed-out pass's own push is refused once; the
#      push before the exit carries the commit all the same
#   8  false, PR open: the end-of-run push that carries three held-back passes says so —
#      branch, three commits, head; 8b by default it confirms the push that found nothing new;
#      8c guard: a dry run, which pushes nothing, confirms nothing; 8d a run that committed
#      nothing and lost its remote-tracking ref still pushes (an unreadable comparison counts
#      as ahead) and its confirmation says the count is unknown
#   9  the end-of-run push is refused once and goes through on its retry: one confirmation,
#      the retry's
#  10  the bound on an interrupt: under the real bureau-runtime.py the provider fails with 130
#      after a commit and the push hangs on origin; a stop (SIGTERM to the runtime) ends the
#      run within its grace with 130, and no process of the push is left
#
# The EXIT trap (since the third round: in both modes, whatever origin lacks):
#  11  the default, under the real runtime: the run is stopped (SIGTERM to the runtime) while
#      the first pass, which has committed, still runs: 130, the EXIT trap pushes the commit
#      once and confirms it; 11b the same with false and a PR open
#  11c the same stop, and the trap's push hangs on origin: the runtime's grace ends it (the
#      run ends 130 after the 4 s grace, not later), and no process of the push is left
#  12  the default, the stage on a terminal that closes during the first pass (hang-up): 129,
#      the EXIT trap pushes the commit although every write to the terminal fails
#  12b the default, stderr a pipe whose reader is gone and SIGTERM to the stage during the
#      first pass: 143, the commit is pushed; 12c the same when origin refuses the push: 143
#      all the same (the refused push's report cannot end the stage with SIGPIPE), one attempt
# A stderr that is gone before the push of a failed pass:
#  13  stderr's reader goes during the first pass, which commits and fails with 1: the stage
#      still ends with 1 (not 141, SIGPIPE), and the commit is pushed
#  13b false, PR open, BUREAU_IMPL_MAX_ITER=1: the same pass times out instead: 124, pushed
#  13c the provider runs out of quota (23) instead and origin refuses the push: 23, not 141,
#      after one attempt, nothing confirmed; 13d the default, BUREAU_IMPL_MAX_ITER=1, the pass
#      times out and origin refuses both its own push and the one before the exit: 124
#  14  the /goal path (agents.use_goal_loop), the default: the run commits and fails with 1;
#      its exit goes through the EXIT trap, which pushes the commit
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr3-doubles.sh"

FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
check_eq() { [ "$1" = "$2" ] || fail "$3: expected '$1', got '$2'"; }
has() { grep -qE -- "$1" <<< "$2" || fail "$3 (no match for /$1/)"; }
hasnt() { if grep -qE -- "$1" <<< "$2"; then fail "$3 (unexpected /$1/)"; fi; }
calls() { cat "$SANDBOX/calls.log" 2>/dev/null || true; }
origin_tip() { git -C "$SANDBOX/.fake-origin.git" rev-parse test-branch; }
head_short() { git -C "$SANDBOX" rev-parse --short HEAD; }
on_origin() { check_eq "$(git -C "$SANDBOX" rev-parse HEAD)" "$(origin_tip)" "$1"; }

setup() {  # setup [implement-config-json] — up to three passes, each with a commit, PARTIAL
  sandbox_init "EXP-100" "test-branch"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt"
  export FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3" BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
  unset BUREAU_USE_GOAL_LOOP FAKE_CLAUDE_COMMIT_MSG GH_STUB_EXISTING_PR PR3_EXIT_ON PR3_EXIT_CODE \
    FAKE_CLAUDE_TIMEOUT_ON_ITERS FAKE_CLAUDE_TIMEOUT_SLEEP BUREAU_IMPL_TOTAL_TIMEOUT PR3_ON_CALL_1
  if [ -n "${1:-}" ]; then
    jq -n --argjson impl "$1" '{agents: {implement: $impl}}' > "$SANDBOX/.bureau.json"
  fi
  pr3_count_pushes
  pr3_count_receives
  pr3_fake_claude
}
OFF='{"enabled": true, "push_each_iteration": false}'

# hook <name> <body>: a hook in the bare origin.
hook() {
  printf '#!/bin/sh\n%s\n' "$2" > "$SANDBOX/.fake-origin.git/hooks/$1"
  chmod +x "$SANDBOX/.fake-origin.git/hooks/$1"
}

# 1, 2 — false, PR open, the first pass commits and the provider fails
for c in "1 1" "2 130"; do
  set -- $c
  setup "$OFF"
  export GH_STUB_EXISTING_PR=7 PR3_EXIT_ON=1 PR3_EXIT_CODE="$2"
  pr3_run_implement
  check_eq "$2" "$LAST_RC" "$1 the provider's exit code is kept"
  check_eq 1 "$(pr3_pushes)" "$1 one push, before the exit"
  check_eq 1 "$(pr3_receives)" "$1 origin received it"
  on_origin "$1 the first pass's commit is on origin"
  has "pushing 1 commit\(s\) of test-branch that origin lacks before the stage ends \(provider exit $2\)" "$LAST_STDERR" "$1 says it pushes, and why"
  has "^  pushed test-branch \(provider exit $2\): 1 commit\(s\) origin lacked, head $(head_short)$" "$LAST_STDOUT" "$1 confirms the push"
  hasnt 'pushing the deferred commits' "$LAST_STDERR" "$1 the EXIT trap has nothing left to push"
  hasnt 'move_issue' "$(calls)" "$1 no hand-off"
  teardown
done

# 3 — the default, PR open, the first pass commits and fails
setup
export GH_STUB_EXISTING_PR=7 PR3_EXIT_ON=1 PR3_EXIT_CODE=1
pr3_run_implement
check_eq 1 "$LAST_RC" "3 exit"
check_eq 1 "$(pr3_pushes)" "3 one push, before the exit"
on_origin "3 the first pass's commit is on origin"
has '^  pushed test-branch \(provider exit 1\): 1 commit\(s\) origin lacked' "$LAST_STDOUT" "3 confirms the push"
teardown

# 4 — false, PR open, origin rejects every push
setup "$OFF"
export GH_STUB_EXISTING_PR=7 PR3_EXIT_ON=1 PR3_EXIT_CODE=1
hook pre-receive 'echo "rejected by test" >&2; exit 1'
pr3_run_implement
check_eq 1 "$LAST_RC" "4 the provider's exit code is kept, no 18"
check_eq 1 "$(pr3_pushes)" "4 one attempt"
has 'PUSH FAILED \(provider exit 1\): branch .test-branch. — git exit [0-9]+' "$LAST_STDERR" "4 the failure is loud"
has 'the work is only in this worktree until a later push succeeds' "$LAST_STDERR" "4 says where the work is"
hasnt 'pushed test-branch' "$LAST_STDOUT" "4 no confirmation"
teardown

# 4b — the comparison with origin cannot be read
setup "$OFF"
export GH_STUB_EXISTING_PR=7 PR3_EXIT_ON=1 PR3_EXIT_CODE=1 PR3_ON_CALL_1="git update-ref -d refs/remotes/origin/test-branch"
pr3_run_implement
check_eq 1 "$LAST_RC" "4b exit"
check_eq 1 "$(pr3_pushes)" "4b one push, before the exit"
on_origin "4b the first pass's commit is on origin"
has 'pushing the commit\(s\) of test-branch that origin lacks before the stage ends \(provider exit 1\)' "$LAST_STDERR" "4b an unreadable comparison counts as ahead"
has "^  pushed test-branch \(provider exit 1\): origin/test-branch could not be compared before the push, head $(head_short)$" "$LAST_STDOUT" "4b the confirmation says the count is unknown"
teardown

# 5 — guard: nothing origin lacks, no push
setup
export PR3_EXIT_ON=2 PR3_EXIT_CODE=1 FAKE_CLAUDE_COMMIT_ON_ITERS=1
pr3_run_implement
check_eq 1 "$LAST_RC" "5 exit"
check_eq 1 "$(pr3_pushes)" "5 only the push after pass 1"
hasnt 'that origin lacks before the stage ends' "$LAST_STDERR" "5 nothing to push"
teardown

# 6 — false, PR open, the only pass commits and times out
setup "$OFF"
export GH_STUB_EXISTING_PR=7 BUREAU_IMPL_MAX_ITER=1 FAKE_CLAUDE_TIMEOUT_ON_ITERS=1 FAKE_CLAUDE_COMMIT_ON_ITERS=1
pr3_run_implement
check_eq 124 "$LAST_RC" "6 exit"
has 'push deferred \(iter 1\)' "$LAST_STDOUT" "6 the pass's push was held back"
check_eq 1 "$(pr3_pushes)" "6 one push, before the exit"
on_origin "6 the timed-out pass's commit is on origin"
has 'pushing 1 commit\(s\) of test-branch that origin lacks before the stage ends \(provider exit 124\)' "$LAST_STDERR" "6 says it pushes"
teardown

# 7 — the same when the total budget leaves no room for another pass
setup "$OFF"
export GH_STUB_EXISTING_PR=7 BUREAU_IMPL_TOTAL_TIMEOUT=66 FAKE_CLAUDE_TIMEOUT_ON_ITERS=1 FAKE_CLAUDE_TIMEOUT_SLEEP=7 \
  FAKE_CLAUDE_COMMIT_ON_ITERS=1
pr3_run_implement
check_eq 124 "$LAST_RC" "7 exit"
has 'no pass is left: the total budget' "$LAST_STDERR" "7 ended on the budget"
check_eq 1 "$(pr3_pushes)" "7 one push, before the exit"
on_origin "7 the timed-out pass's commit is on origin"
teardown

# 6b — the default, the only pass times out and its own push is refused once
setup
export BUREAU_IMPL_MAX_ITER=1 FAKE_CLAUDE_TIMEOUT_ON_ITERS=1 FAKE_CLAUDE_COMMIT_ON_ITERS=1
hook pre-receive "if [ -f '$SANDBOX/.refused-once' ]; then exit 0; fi; : > '$SANDBOX/.refused-once'; echo 'refused once by test' >&2; exit 1"
pr3_run_implement
check_eq 124 "$LAST_RC" "6b exit"
has 'PUSH FAILED \(iter 1\)' "$LAST_STDERR" "6b the pass's own push was refused"
check_eq 2 "$(pr3_pushes)" "6b the pass's push and the one before the exit"
on_origin "6b the timed-out pass's commit is on origin"
teardown

# 8 — the end-of-run push confirms itself
setup "$OFF"
export GH_STUB_EXISTING_PR=7
pr3_run_implement
check_eq 0 "$LAST_RC" "8 exit"
check_eq 1 "$(pr3_pushes)" "8 one push, at the end"
has "^  pushed test-branch \(end of run\): 3 commit\(s\) origin lacked, head $(head_short)$" "$LAST_STDOUT" "8 branch, count and head"
check_eq 1 "$(printf '%s\n' "$LAST_STDOUT" | grep -c '^  pushed test-branch' || true)" "8 one confirmation"
teardown

# 8b — by default the passes were pushed already; the end-of-run push says origin lacked nothing
setup
pr3_run_implement
check_eq 4 "$(pr3_pushes)" "8b three passes and the end of the run"
has "^  pushed test-branch \(end of run\): 0 commit\(s\) origin lacked, head $(head_short)$" "$LAST_STDOUT" "8b confirms the head on origin"
teardown

# 8c — guard: a dry run confirms nothing
setup "$OFF"
export GH_STUB_EXISTING_PR=7 BUREAU_DRY_RUN=1
pr3_run_implement
has '\[DRY_RUN\] would: git push' "$LAST_STDOUT" "8c the dry run reaches the end-of-run push"
hasnt 'pushed test-branch' "$LAST_STDOUT" "8c no confirmation"
teardown

# 8d — nothing committed, the remote-tracking ref gone: the end-of-run push still goes out
setup "$OFF"
export GH_STUB_EXISTING_PR=7 FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="" \
  PR3_ON_CALL_1="git update-ref -d refs/remotes/origin/test-branch"
pr3_run_implement
check_eq 0 "$LAST_RC" "8d exit"
has 'terminal status=COMPLETE' "$LAST_STDOUT" "8d COMPLETE without a commit of this run"
check_eq 1 "$(pr3_pushes)" "8d the end-of-run push"
has "^  pushed test-branch \(end of run\): origin/test-branch could not be compared before the push, head $(head_short)$" "$LAST_STDOUT" "8d the count is unknown"
teardown

# 9 — the end-of-run push is refused once, its retry goes through
setup "$OFF"
export GH_STUB_EXISTING_PR=7
hook pre-receive "if [ -f '$SANDBOX/.refused-once' ]; then exit 0; fi; : > '$SANDBOX/.refused-once'; echo 'refused once by test' >&2; exit 1"
pr3_run_implement
check_eq 0 "$LAST_RC" "9 exit"
check_eq 2 "$(pr3_pushes)" "9 the push and its retry"
on_origin "9 every commit on origin"
has "^  pushed test-branch \(end of run, retry\): 3 commit\(s\) origin lacked, head $(head_short)$" "$LAST_STDOUT" "9 the retry confirms"
hasnt '^  pushed test-branch \(end of run\):' "$LAST_STDOUT" "9 the refused push is not confirmed"
teardown

# 10 — the bound on an interrupt, under the real runtime. The provider fails with 130 after a
# commit; origin's pre-receive hook records its pid and hangs, so the push hangs. Then the run
# is stopped the way an operator does (SIGTERM to the runtime in front, grace step 1 s, so the
# runtime would kill the stage's group after 4 s).
setup
export PR3_EXIT_ON=1 PR3_EXIT_CODE=130 FAKE_CLAUDE_COMMIT_ON_ITERS=1
echo '{}' > "$SANDBOX/.bureau.json"
hook pre-receive "echo \$\$ > '$SANDBOX/.hook-pid'; exec sleep 60"
(cd "$SANDBOX" && exec env -u BUREAU_RUN_ID -u BUREAU_RUN_DEPTH -u BUREAU_ACTIVE_ENTRY -u BUREAU_CONFIG \
   PATH="$SANDBOX/.pr3-bin:$PATH" BUREAU_STOP_GRACE_SECONDS=1 \
   python3 -I "$SCRIPTS_DIR/bureau-runtime.py" --repo "$SANDBOX" exec --issue EXP-100 -- \
   bash "$SCRIPTS_DIR/implement-pipeline.sh" > "$SANDBOX/runtime.out" 2> "$SANDBOX/runtime.err") &
RUNTIME=$!
n=0
while [ ! -s "$SANDBOX/.hook-pid" ] && kill -0 "$RUNTIME" 2>/dev/null && [ "$n" -lt 300 ]; do sleep 0.1; n=$((n + 1)); done
if [ ! -s "$SANDBOX/.hook-pid" ]; then
  fail "10 the push never started (stderr: $(tr '\n' '|' < "$SANDBOX/runtime.err"))"
  kill -KILL "$RUNTIME" 2>/dev/null || true
  wait "$RUNTIME" 2>/dev/null || true
else
  HOOK_PID=$(cat "$SANDBOX/.hook-pid")
  started=$(date +%s)
  kill -TERM "$RUNTIME"
  n=0
  while kill -0 "$RUNTIME" 2>/dev/null && [ "$n" -lt 300 ]; do sleep 0.1; n=$((n + 1)); done
  if kill -0 "$RUNTIME" 2>/dev/null; then
    fail "10 the runtime still runs 30 s after the stop"
    kill -KILL "$RUNTIME" 2>/dev/null || true
  fi
  set +e; wait "$RUNTIME"; rc=$?; set -e
  took=$(( $(date +%s) - started ))
  check_eq 130 "$rc" "10 the run ends as interrupted"
  [ "$took" -le 6 ] || fail "10 the stop took ${took}s, more than its 4 s grace and 2 s slack"
  n=0
  while kill -0 "$HOOK_PID" 2>/dev/null && [ "$n" -lt 20 ]; do sleep 0.1; n=$((n + 1)); done
  if kill -0 "$HOOK_PID" 2>/dev/null; then
    fail "10 a process of the push outlived the stop"
    kill -KILL "$HOOK_PID" 2>/dev/null || true
  fi
  has 'pushing 1 commit\(s\) of test-branch that origin lacks before the stage ends \(provider exit 130\)' \
    "$(cat "$SANDBOX/runtime.err")" "10 the push was the stage's"
fi
teardown

# ── The EXIT trap, and a stderr that is gone ───────────────────────────────────────────────
# HOLD: the first pass, after its commit, waits (up to 30 s) until the case releases it or a
# signal ends it, and says so with .pr3-in-pass.
HOLD=': > .pr3-in-pass; i=0; while [ ! -f .pr3-stage-stopped ] && [ "$i" -lt 300 ]; do sleep 0.1; i=$((i + 1)); done'

# runtime_start: the stage under the real bureau-runtime.py exec (grace step 1 s: 4 s at the
# front), in the background. RUNTIME is its pid, the only process these cases signal.
runtime_start() {
  [ -f "$SANDBOX/.bureau.json" ] || echo '{}' > "$SANDBOX/.bureau.json"
  (cd "$SANDBOX" && exec env -u BUREAU_RUN_ID -u BUREAU_RUN_DEPTH -u BUREAU_ACTIVE_ENTRY -u BUREAU_CONFIG \
     PATH="$SANDBOX/.pr3-bin:$PATH" BUREAU_STOP_GRACE_SECONDS=1 \
     python3 -I "$SCRIPTS_DIR/bureau-runtime.py" --repo "$SANDBOX" exec --issue EXP-100 -- \
     bash "$SCRIPTS_DIR/implement-pipeline.sh" > "$SANDBOX/runtime.out" 2> "$SANDBOX/runtime.err") &
  RUNTIME=$!
}
# fifo_stage_start: the stage alone, stdout to a file, stderr to a pipe whose reader (READER)
# a case can end, as a queue loop's tee ends with a hang-up. STAGE is the stage's pid.
fifo_stage_start() {
  mkfifo "$SANDBOX/.pr3-stderr"
  cat "$SANDBOX/.pr3-stderr" > "$SANDBOX/stderr.log" &
  READER=$!
  (cd "$SANDBOX" && PATH="$SANDBOX/.pr3-bin:$PATH" exec bash "$SCRIPTS_DIR/implement-pipeline.sh" \
     > "$SANDBOX/stdout.log" 2> "$SANDBOX/.pr3-stderr") &
  STAGE=$!
}
# wait_file <file> <pid>: up to 30 s for the file while the process runs.
wait_file() {
  local n=0
  while [ ! -e "$1" ] && kill -0 "$2" 2>/dev/null && [ "$n" -lt 300 ]; do sleep 0.1; n=$((n + 1)); done
  [ -e "$1" ]
}
# finish <label> <pid>: wait up to 30 s for the process (SIGKILL to it and a failure after
# that); RC is its exit code, TOOK the seconds since STARTED.
finish() {
  local n=0
  { while kill -0 "$2" && [ "$n" -lt 300 ]; do sleep 0.1; n=$((n + 1)); done; } 2>/dev/null
  if kill -0 "$2" 2>/dev/null; then fail "$1 still runs after 30 s"; kill -KILL "$2" 2>/dev/null || true; fi
  set +e; { wait "$2"; RC=$?; } 2>/dev/null; set -e
  TOOK=$(( $(date +%s) - STARTED ))
}
# quiet: wait up to 10 s until no process of the sandbox runs any more (lookups only).
quiet() {
  local n=0
  : > "$SANDBOX/.pr3-stage-stopped"
  while ps -A -o args= | grep -F "$SANDBOX/" | grep -v grep >/dev/null && [ "$n" -lt 100 ]; do sleep 0.1; n=$((n + 1)); done
}
TRAP_WHY='the stage ends before its end-of-run push'

# 11, 11b — the run is stopped during the first pass, which has committed
for c in "11 default" "11b false"; do
  set -- $c
  if [ "$2" = false ]; then setup "$OFF"; export GH_STUB_EXISTING_PR=7; else setup; fi
  export FAKE_CLAUDE_COMMIT_ON_ITERS=1 PR3_ON_CALL_1="$HOLD"
  runtime_start
  if wait_file "$SANDBOX/.pr3-in-pass" "$RUNTIME"; then
    STARTED=$(date +%s); kill -TERM "$RUNTIME"; finish "$1 the runtime" "$RUNTIME"
    quiet
    check_eq 130 "$RC" "$1 the run ends as interrupted"
    check_eq 1 "$(pr3_pushes)" "$1 one push, from the EXIT trap"
    on_origin "$1 the first pass's commit is on origin"
    has "pushing 1 commit\(s\) of test-branch that origin lacks before the stage ends \($TRAP_WHY\)" "$(cat "$SANDBOX/runtime.err")" "$1 says why"
    has "^  pushed test-branch \($TRAP_WHY\): 1 commit\(s\) origin lacked, head $(head_short)$" "$(cat "$SANDBOX/runtime.out")" "$1 confirms the push"
  else
    fail "$1 the first pass never started ($(tr '\n' '|' < "$SANDBOX/runtime.err"))"
    kill -KILL "$RUNTIME" 2>/dev/null || true; wait "$RUNTIME" 2>/dev/null || true
  fi
  teardown
done

# 11c — the trap's push hangs on origin: the runtime's grace ends it
setup
export FAKE_CLAUDE_COMMIT_ON_ITERS=1 PR3_ON_CALL_1="$HOLD"
hook pre-receive "echo \$\$ > '$SANDBOX/.hook-pid'; exec sleep 60"
runtime_start
if wait_file "$SANDBOX/.pr3-in-pass" "$RUNTIME"; then
  STARTED=$(date +%s); kill -TERM "$RUNTIME"; finish "11c the runtime" "$RUNTIME"
  check_eq 130 "$RC" "11c the run ends as interrupted"
  [ -s "$SANDBOX/.hook-pid" ] || fail "11c the EXIT trap's push never reached origin"
  [ "$TOOK" -ge 3 ] && [ "$TOOK" -le 7 ] || fail "11c the stop took ${TOOK}s; the hanging push should hold it for the 4 s grace and no longer"
  if [ -s "$SANDBOX/.hook-pid" ]; then
    HOOK_PID=$(cat "$SANDBOX/.hook-pid"); n=0
    while kill -0 "$HOOK_PID" 2>/dev/null && [ "$n" -lt 20 ]; do sleep 0.1; n=$((n + 1)); done
    if kill -0 "$HOOK_PID" 2>/dev/null; then fail "11c a process of the push outlived the stop"; kill -KILL "$HOOK_PID" 2>/dev/null || true; fi
  fi
  quiet
else
  fail "11c the first pass never started"; kill -KILL "$RUNTIME" 2>/dev/null || true; wait "$RUNTIME" 2>/dev/null || true
fi
teardown

# 12 — the default, the stage's terminal closes during the first pass
setup
export FAKE_CLAUDE_COMMIT_ON_ITERS=1 PR3_ON_CALL_1="$HOLD"
result=$(cd "$SANDBOX" && PATH="$SANDBOX/.pr3-bin:$PATH" python3 "$LIB_DIR/hangup.py" --ready "$SANDBOX/.pr3-in-pass" \
  --how close --hung-up "$SANDBOX/.pr3-stage-stopped" --out "$SANDBOX/stdout.log" -- bash "$SCRIPTS_DIR/implement-pipeline.sh") || true
quiet
check_eq 129 "$(sed -n 's/^rc=\([0-9]*\) .*/\1/p' <<< "$result")" "12 ended by the hang-up ($result)"
check_eq 1 "$(pr3_pushes)" "12 one push, from the EXIT trap"
on_origin "12 the first pass's commit is on origin"
has "^  pushed test-branch \($TRAP_WHY\): 1 commit\(s\) origin lacked" "$(cat "$SANDBOX/stdout.log")" "12 confirms the push"
teardown

# 12b, 12c — the default, stderr's reader is gone and the stage gets SIGTERM during the first
# pass; in 12c origin refuses the push
for c in "12b accept" "12c refuse"; do
  set -- $c
  setup
  export FAKE_CLAUDE_COMMIT_ON_ITERS=1 PR3_ON_CALL_1="$HOLD"
  [ "$2" = accept ] || hook pre-receive 'echo "rejected by test" >&2; exit 1'
  fifo_stage_start
  if wait_file "$SANDBOX/.pr3-in-pass" "$STAGE"; then
    kill "$READER" 2>/dev/null || true; wait "$READER" 2>/dev/null || true
    # Only the stage gets the signal here, as in case 20c of test_implement_push_open_pr.sh: bash
    # acts on it once the provider call it waits for returns, so the pass is released too.
    STARTED=$(date +%s); kill -TERM "$STAGE"; : > "$SANDBOX/.pr3-stage-stopped"; finish "$1 the stage" "$STAGE"
    quiet
    check_eq 143 "$RC" "$1 ended by SIGTERM"
    check_eq 1 "$(pr3_pushes)" "$1 one push, from the EXIT trap"
    if [ "$2" = accept ]; then on_origin "$1 the first pass's commit is on origin"
    else hasnt '^  pushed ' "$(cat "$SANDBOX/stdout.log")" "$1 a refused push is not confirmed"; fi
  else
    fail "$1 the first pass never started"; kill -KILL "$STAGE" "$READER" 2>/dev/null || true
  fi
  teardown
done

# 13, 13b, 13c, 13d — stderr's reader goes during the first pass, which then fails (1, 23) or
# times out (124, with false and a PR open, or by default); in 13c and 13d origin refuses every
# push. Fields: case, exit code, push mode, origin, push attempts.
for c in "13 1 default accept 1" "13b 124 false accept 1" "13c 23 default refuse 1" "13d 124 default refuse 2"; do
  set -- $c
  if [ "$3" = false ]; then setup "$OFF"; export GH_STUB_EXISTING_PR=7; else setup; fi
  if [ "$2" = 124 ]; then
    export BUREAU_IMPL_MAX_ITER=1 FAKE_CLAUDE_TIMEOUT_ON_ITERS=1
  else
    export PR3_EXIT_ON=1 PR3_EXIT_CODE="$2"
  fi
  [ "$4" = accept ] || hook pre-receive 'echo "rejected by test" >&2; exit 1'
  export FAKE_CLAUDE_COMMIT_ON_ITERS=1 PR3_ON_CALL_1="$HOLD"
  fifo_stage_start
  if wait_file "$SANDBOX/.pr3-in-pass" "$STAGE"; then
    kill "$READER" 2>/dev/null || true; wait "$READER" 2>/dev/null || true
    STARTED=$(date +%s); : > "$SANDBOX/.pr3-stage-stopped"; finish "$1 the stage" "$STAGE"
    quiet
    check_eq "$2" "$RC" "$1 the provider's exit code is kept, no SIGPIPE"
    check_eq "$5" "$(pr3_pushes)" "$1 push attempts"
    if [ "$4" = accept ]; then
      on_origin "$1 the first pass's commit is on origin"
      has "^  pushed test-branch \(provider exit $2\): 1 commit\(s\) origin lacked" "$(cat "$SANDBOX/stdout.log")" "$1 confirms the push"
    else
      hasnt '^  pushed ' "$(cat "$SANDBOX/stdout.log")" "$1 a refused push is not confirmed"
    fi
  else
    fail "$1 the first pass never started"; kill -KILL "$STAGE" "$READER" 2>/dev/null || true
  fi
  teardown
done

# 14 — the /goal path: a failed run's commit goes out through the EXIT trap
setup
export BUREAU_USE_GOAL_LOOP=1 FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS=1 \
  PR3_EXIT_ON=1 PR3_EXIT_CODE=1
pr3_run_implement
check_eq 1 "$LAST_RC" "14 the provider's exit code is kept"
check_eq 1 "$(pr3_pushes)" "14 one push, from the EXIT trap"
on_origin "14 the run's commit is on origin"
has "^  pushed test-branch \($TRAP_WHY\): 1 commit\(s\) origin lacked, head $(head_short)$" "$LAST_STDOUT" "14 confirms the push"
teardown

trap - EXIT  # every case tore its own sandbox down
if [ "$FAILS" -gt 0 ]; then
  echo "test_implement_failed_pass_push: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_implement_failed_pass_push"
