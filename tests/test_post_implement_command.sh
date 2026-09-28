#!/bin/bash
# repo.post_implement_command and the fatal end-of-run push, on the real
# implement stage (tests/lib/harness.sh: stubbed Linear/gh/model, real git with
# a bare origin).
#
#   1  hook unset → today's behaviour (COMPLETE, Build Review, exit 0)
#   2  hook set, run made commits → runs once, in the worktree, with
#      BUREAU_ISSUE/BUREAU_BRANCH; its commit counts, is pushed, hand-off as usual
#   3  hook set, run made no commits → not run
#   4  hook exits non-zero → halt (needs-human, draft, report), exit 14, no hand-off
#   5  hook leaves uncommitted changes → halt, exit 14, files kept and named
#   6  hook exceeds BUREAU_POST_IMPLEMENT_TIMEOUT → killed, halt, exit 14
#   7  hook moves HEAD off the run's commits → halt, exit 14, final push skipped
#   8  dry run → hook not run
#   9  hook commit carries a CI suppressor → squash-range check halts (CI_MARKER)
#  10  every push rejected → stage ends 18 before any hand-off
#  11  only the first push rejected → per-iter push stays non-fatal, exit 0
#  12  goal-loop path runs the hook too
#  14  the hook gets no stdin
#  15  hook fails and the label write fails → 25 (the hold) wins over 14
#  13  negative control: the pre-change stage (hook call removed, final push
#      non-fatal) shows the old behaviour for 2 and 10
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
check_eq() { [ "$1" = "$2" ] || fail "$3: expected '$1', got '$2'"; }
has() { printf '%s' "$2" | grep -qE -- "$1" || fail "$3 (no match for /$1/)"; }
hasnt() { if printf '%s' "$2" | grep -qE -- "$1"; then fail "$3 (unexpected /$1/)"; fi; }
calls() { cat "$SANDBOX/calls.log" 2>/dev/null || true; }

MARK_DIR=$(mktemp -d -t bureau-postimpl-mark.XXXXXX)
export MARK_DIR

setup() {  # setup <case-name> [hook-command]
  sandbox_init "EXP-100" "test-branch"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
  export FAKE_CLAUDE_COMMIT_ON_ITERS="1" BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
  unset BUREAU_USE_GOAL_LOOP BUREAU_POST_IMPLEMENT_TIMEOUT FAKE_CLAUDE_COMMIT_MSG
  export MARK="$MARK_DIR/$1"
  rm -f "$MARK"
  if [ -n "${2:-}" ]; then
    jq -n --arg c "$2" '{repo: {post_implement_command: $c}}' > "$SANDBOX/.bureau.json"
  fi
}
reject_pushes() {  # reject_pushes all|first
  local hook="$SANDBOX/.fake-origin.git/hooks/pre-receive"
  if [ "$1" = all ]; then
    printf '#!/bin/sh\necho "rejected by test" >&2\nexit 1\n' > "$hook"
  else
    printf '#!/bin/sh\nf="%s/.rejected-once"\n[ -f "$f" ] && exit 0\n: > "$f"\necho "rejected once by test" >&2\nexit 1\n' "$SANDBOX" > "$hook"
  fi
  chmod +x "$hook"
}
origin_subjects() { git -C "$SANDBOX/.fake-origin.git" log --format=%s test-branch 2>/dev/null || true; }
# The pre-change stage: no hook call, end-of-run push non-fatal again.
use_old_stage() {
  local f="$SCRIPTS_DIR/implement-pipeline.sh"
  grep -q '^run_post_implement_command$' "$f" || { fail "negative control: hook call line not found"; return; }
  grep -q 'elif ! push_branch_loud "end of run" fatal; then' "$f" || { fail "negative control: fatal push line not found"; return; }
  sed -i.bak -e '/^run_post_implement_command$/d' \
    -e 's/elif ! push_branch_loud "end of run" fatal; then/elif ! push_branch_loud "end of run"; then/' "$f"
}

HOOK_OK='printf "%s %s %s\n" "$BUREAU_ISSUE" "$BUREAU_BRANCH" "$(pwd -P)" >> "$MARK"; date > generated.txt; git add generated.txt; git commit -qm "chore: regenerate derived files"'

# 1 — unset
setup c1
run_implement_pipeline
check_eq 0 "$LAST_RC" "1 exit"
has 'status=COMPLETE' "$LAST_STDOUT" "1 COMPLETE"
hasnt 'post_implement' "$LAST_STDOUT$LAST_STDERR" "1 no hook output"
has 'move_issue.*state-build-review' "$(calls)" "1 hand-off"
teardown

# 2 — runs once in the worktree, commit counted and pushed
setup c2 "$HOOK_OK"
run_implement_pipeline
check_eq 0 "$LAST_RC" "2 exit"
check_eq 1 "$(wc -l < "$MARK" 2>/dev/null | tr -d ' ' || echo 0)" "2 hook ran exactly once"
check_eq "EXP-100 test-branch $(cd "$SANDBOX" && pwd -P)" "$(cat "$MARK" 2>/dev/null)" "2 env and working directory"
has 'repo.post_implement_command: ok, 1 commit' "$LAST_STDOUT" "2 hook commit counted"
has 'chore: regenerate derived files' "$(origin_subjects)" "2 hook commit on origin"
has 'move_issue.*state-build-review' "$(calls)" "2 hand-off"
hasnt 'needs-human' "$(calls)" "2 no needs-human"
teardown

# 3 — no commits in this run → not run
setup c3 "$HOOK_OK"
export FAKE_CLAUDE_COMMIT_ON_ITERS=""
run_implement_pipeline
[ ! -e "$MARK" ] || fail "3 hook ran although the run made no commits"
has 'post_implement_command: skipped \(this run made no commits\)' "$LAST_STDOUT" "3 skip line"
teardown

# 4 — non-zero exit
setup c4 'echo "regen broke on purpose"; exit 3'
run_implement_pipeline
check_eq 14 "$LAST_RC" "4 exit"
hasnt 'move_issue' "$(calls)" "4 no state move"
has 'add_issue_label.*needs-human' "$(calls)" "4 needs-human"
has 'post_comment.*repo.post_implement_command failed' "$(calls)" "4 halt comment"
has 'repo.post_implement_command exited 3' "$(calls)" "4 report names the exit"
has 'regen broke on purpose' "$(calls)" "4 report carries the output"
has 'fake-claude iter 1 progress' "$(origin_subjects)" "4 run's commits still pushed"
teardown

# 5 — uncommitted changes
setup c5 'echo derived > left-behind.txt; echo more >> iter_1_progress.txt'
run_implement_pipeline
check_eq 14 "$LAST_RC" "5 exit"
has 'left changes it did not commit' "$(calls)" "5 reason"
has 'left-behind.txt' "$(calls)" "5 untracked file named"
has 'iter_1_progress.txt' "$(calls)" "5 modified file named"
[ -f "$SANDBOX/left-behind.txt" ] || fail "5 the hook's file was deleted"
hasnt 'move_issue' "$(calls)" "5 no state move"
teardown

# 6 — timeout kills the hook's process group
setup c6 'echo $$ > "$MARK.pid"; sleep 60 & echo $! >> "$MARK.pid"; wait'
export BUREAU_POST_IMPLEMENT_TIMEOUT=2
t0=$(date +%s)
run_implement_pipeline
t1=$(date +%s)
check_eq 14 "$LAST_RC" "6 exit"
has 'timed out after 2s' "$(calls)" "6 reason"
[ $((t1 - t0)) -lt 30 ] || fail "6 stage waited $((t1 - t0))s for the hook"
if [ -f "$MARK.pid" ]; then
  while read -r pid; do
    if kill -0 "$pid" 2>/dev/null; then fail "6 hook process $pid survived the timeout"; kill "$pid" 2>/dev/null || true; fi
  done < "$MARK.pid"
else
  fail "6 hook never started"
fi
rm -f "$MARK.pid"
teardown

# 7 — HEAD moved off the run's commits: no push of rewritten history
setup c7 'git reset -q --hard HEAD~1'
run_implement_pipeline
check_eq 14 "$LAST_RC" "7 exit"
has "moved HEAD off the run's last commit" "$(calls)" "7 reason"
has 'not pushing: repo.post_implement_command moved HEAD' "$LAST_STDERR" "7 final push skipped"
has 'fake-claude iter 1 progress' "$(origin_subjects)" "7 origin keeps the run's commit"
teardown

# 8 — dry run
setup c8 "$HOOK_OK"
export BUREAU_DRY_RUN=1
run_implement_pipeline
[ ! -e "$MARK" ] || fail "8 hook ran in a dry run"
has '\[DRY_RUN\] would run repo.post_implement_command' "$LAST_STDOUT" "8 dry-run line"
teardown

# 9 — a CI suppressor in the hook's commit is caught by the squash-range check
setup c9 'date > generated.txt; git add generated.txt; git commit -qm "chore: regenerate [skip ci]"'
run_implement_pipeline
hasnt 'move_issue' "$(calls)" "9 no hand-off"
has 'post_comment.*Halted before hand-off: a commit in the squash range' "$(calls)" "9 CI_MARKER halt"
teardown

# 10 — every push rejected: 18, nothing handed on
setup c10
reject_pushes all
run_implement_pipeline
check_eq 18 "$LAST_RC" "10 exit"
hasnt 'move_issue' "$(calls)" "10 no state move"
hasnt 'Implementation complete' "$(calls)" "10 no completion comment"
has 'post_comment.*final push of `test-branch` to origin failed' "$(calls)" "10 comment"
has 'PUSH FAILED \(end of run\)' "$LAST_STDERR" "10 loud push line"
teardown

# 11 — a failed per-iter push stays non-fatal; the end-of-run push retries it
setup c11
reject_pushes first
run_implement_pipeline
check_eq 0 "$LAST_RC" "11 exit"
has 'PUSH FAILED \(iter 1\)' "$LAST_STDERR" "11 first push failed"
has 'move_issue.*state-build-review' "$(calls)" "11 hand-off"
has 'fake-claude iter 1 progress' "$(origin_subjects)" "11 commit on origin"
teardown

# 12 — the goal-loop path runs the hook too
setup c12 "$HOOK_OK"
export BUREAU_USE_GOAL_LOOP=1
run_implement_pipeline
check_eq 1 "$(wc -l < "$MARK" 2>/dev/null | tr -d ' ' || echo 0)" "12 hook ran on the goal path"
has 'chore: regenerate derived files' "$(origin_subjects)" "12 hook commit on origin"
teardown

# 14 — the hook gets no stdin, even when the stage has one
setup c14 'if read -r line; then echo "stdin:$line" >> "$MARK"; fi; echo done >> "$MARK"; date > generated.txt; git add generated.txt; git commit -qm "chore: regenerate derived files"'
( cd "$SANDBOX" && printf 'from-the-stage-stdin\n' | bash "$SCRIPTS_DIR/implement-pipeline.sh" >/dev/null 2>&1 ) || true
check_eq done "$(cat "$MARK" 2>/dev/null)" "14 hook read nothing from stdin"
teardown

# 15 — hook fails and the needs-human label cannot be written: the hold's 25 wins over 14
setup c15 'exit 3'
export BUREAU_STUB_ADD_LABEL_RC=1
run_implement_pipeline
unset BUREAU_STUB_ADD_LABEL_RC
check_eq 25 "$LAST_RC" "15 exit when the escalation could not be labelled"
has 'repo.post_implement_command exited 3' "$(calls)" "15 report still posted"
teardown

# 13 — negative control: the pre-change stage
setup c13a "$HOOK_OK"
use_old_stage
run_implement_pipeline
[ ! -e "$MARK" ] || fail "13 old stage ran the hook (control is not the old stage)"
teardown
setup c13b
reject_pushes all
use_old_stage
run_implement_pipeline
check_eq 0 "$LAST_RC" "13 old stage ends 0 on a failed final push"
has 'move_issue.*state-build-review' "$(calls)" "13 old stage hands on work that is not on origin"
teardown

rm -rf "$MARK_DIR"
trap - EXIT  # every case tore its own sandbox down; the harness trap would fail on the missing one under set -e
if [ "$FAILS" -gt 0 ]; then
  echo "test_post_implement_command: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_post_implement_command"
