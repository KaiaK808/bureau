#!/bin/bash
# repo.post_implement_command and files that were already uncommitted before it ran, on the
# real implement stage (tests/lib/harness.sh: stubbed Linear/gh/model, real git with a bare
# origin). A path the agent left uncommitted keeps the same `git status` line however much
# the hook adds to it; the stage compares its content before and after the hook.
#
#   1  the agent leaves ` M a.txt`, the hook appends to it without committing → 14, named
#   2  the hook leaves the agent's ` M a.txt` alone (and commits its own file) → ok, hand-off
#   3  an untracked file the agent left, appended to by the hook → 14, named
#   4  the hook changes the agent's file and commits it → ok
#   5  the hook rewrites the agent's file with the same bytes → ok
#   6  a path with spaces in a directory with spaces → 14, named in full
#   7  a staged rename with further edits (`RM` in git status), edited again by the hook → 14
#   8  a hook that also leaves a new file → both lists in the report
#  10  a process the agent left running keeps appending to its untracked log while the hook
#      runs: not counted (it still changes after the hook ended), named on stderr, ok
#  11  the agent's untracked symlink to a.txt; the hook changes and commits a.txt → ok
#  12  the same symlink pointed elsewhere by the hook → 14, named
#   9  negative control: the stage without the content comparison passes case 1 (exit 0,
#      hand-off), which is what v3.0.2 did
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr3-doubles.sh"

FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
check_eq() { [ "$1" = "$2" ] || fail "$3: expected '$1', got '$2'"; }
has() { printf '%s' "$2" | grep -qE -- "$1" || { fail "$3 (no match for /$1/)"; report >&2; }; }
report() { calls | sed -n '/Post-implement command:/,$p' | sed 's/^/  | /'; }
hasnt() { if printf '%s' "$2" | grep -qE -- "$1"; then fail "$3 (unexpected /$1/)"; fi; }
calls() { cat "$SANDBOX/calls.log" 2>/dev/null || true; }

setup() {  # setup <hook-command> — tracked a.txt, old.txt and "dir with space/b c.txt" on the branch
  sandbox_init "EXP-100" "test-branch"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
  export FAKE_CLAUDE_COMMIT_ON_ITERS="1" BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
  unset BUREAU_USE_GOAL_LOOP BUREAU_POST_IMPLEMENT_TIMEOUT BUREAU_IMPL_TOTAL_TIMEOUT PR3_DIRTY PR3_RENAME PR3_MARKER_ON PR3_ON_CALL_1
  mkdir -p "$SANDBOX/dir with space"
  printf 'a\n' > "$SANDBOX/a.txt"
  printf 'old\n' > "$SANDBOX/old.txt"
  printf 'b\n' > "$SANDBOX/dir with space/b c.txt"
  git -C "$SANDBOX" add a.txt old.txt "dir with space/b c.txt"
  git -C "$SANDBOX" commit -qm "tracked files for the test"
  git -C "$SANDBOX" push -q origin test-branch
  jq -n --arg c "$1" '{repo: {post_implement_command: $c}}' > "$SANDBOX/.bureau.json"
  pr3_fake_claude
  pr3_ignore_harness_files
}

# 1 — the agent's ` M a.txt`, appended to by the hook
setup 'echo "added by the hook" >> a.txt'
export PR3_DIRTY="a.txt"
pr3_run_implement
check_eq 14 "$LAST_RC" "1 exit"
has 'left changes it did not commit' "$(calls)" "1 reason"
has 'that it changed further \(kept in the worktree\):' "$(calls)" "1 report heading"
has 'changed further \(kept in the worktree\):.a\.txt' "$(calls | tr '\n' ' ')" "1 a.txt named under the heading"
hasnt 'uncommitted changes it left' "$(calls)" "1 no new status line to list"
hasnt 'move_issue' "$(calls)" "1 no hand-off"
has 'add_issue_label.*needs-human' "$(calls)" "1 needs-human"
grep -q 'added by the hook' "$SANDBOX/a.txt" || fail "1 the hook's change was not kept in the worktree"
teardown

# 2 — the agent's ` M a.txt` stays as it was; the hook commits its own file
setup 'date > generated.txt; git add generated.txt; git commit -qm "chore: regenerate"'
export PR3_DIRTY="a.txt"
pr3_run_implement
check_eq 0 "$LAST_RC" "2 exit"
has 'repo.post_implement_command: ok, 1 commit' "$LAST_STDOUT" "2 hook ok"
has 'move_issue.*state-build-review' "$(calls)" "2 hand-off"
check_eq ' M a.txt' "$(git -C "$SANDBOX" status --porcelain -- a.txt)" "2 the agent's change is still there, untouched"
teardown

# 3 — an untracked file the agent left, appended to by the hook
setup 'echo "added by the hook" >> notes.txt'
export PR3_DIRTY="notes.txt"
pr3_run_implement
check_eq 14 "$LAST_RC" "3 exit"
has 'changed further \(kept in the worktree\):.notes\.txt' "$(calls | tr '\n' ' ')" "3 notes.txt named"
teardown

# 4 — the hook changes the agent's file and commits it: nothing is left uncommitted
setup 'echo "added by the hook" >> a.txt; git add a.txt; git commit -qm "chore: fold in a.txt"'
export PR3_DIRTY="a.txt"
pr3_run_implement
check_eq 0 "$LAST_RC" "4 exit"
has 'repo.post_implement_command: ok, 1 commit' "$LAST_STDOUT" "4 hook ok"
has 'move_issue.*state-build-review' "$(calls)" "4 hand-off"
teardown

# 5 — same bytes written again
setup 'cp a.txt .same && cat .same > a.txt && rm .same'
export PR3_DIRTY="a.txt"
pr3_run_implement
check_eq 0 "$LAST_RC" "5 exit"
has 'repo.post_implement_command: ok, 0 commit' "$LAST_STDOUT" "5 hook ok"
teardown

# 6 — spaces in the directory and the file name
setup 'echo "added by the hook" >> "dir with space/b c.txt"'
export PR3_DIRTY="dir with space/b c.txt"
pr3_run_implement
check_eq 14 "$LAST_RC" "6 exit"
has 'changed further \(kept in the worktree\):.dir with space/b c\.txt' "$(calls | tr '\n' ' ')" "6 full path named"
teardown

# 7 — a staged rename the agent edited further (git status: `RM new.txt`), edited again by the hook
setup 'echo "added by the hook" >> new.txt'
export PR3_RENAME="old.txt:new.txt" PR3_DIRTY="new.txt"
pr3_run_implement
check_eq 14 "$LAST_RC" "7 exit"
has 'changed further \(kept in the worktree\):.new\.txt' "$(calls | tr '\n' ' ')" "7 renamed file named"
teardown

# 8 — the hook changes the agent's file and also leaves a new one
setup 'echo "added by the hook" >> a.txt; echo derived > derived.txt'
export PR3_DIRTY="a.txt"
pr3_run_implement
check_eq 14 "$LAST_RC" "8 exit"
has 'uncommitted changes it left \(kept in the worktree\):.\?\? derived\.txt' "$(calls | tr '\n' ' ')" "8 new file listed"
has 'changed further \(kept in the worktree\):.a\.txt' "$(calls | tr '\n' ' ')" "8 changed file listed"
teardown

# 10 — a background writer the agent left behind is not the hook's doing
setup 'sleep 1'
export PR3_ON_CALL_1='echo start > agent-bg.log; nohup sh -c "i=0; while [ ! -e .pr3-stop ] && [ \$i -lt 150 ]; do echo x >> agent-bg.log; sleep 0.2; i=\$((i+1)); done" >/dev/null 2>&1 &'
pr3_run_implement
: > "$SANDBOX/.pr3-stop"
check_eq 0 "$LAST_RC" "10 exit"
has 'not counted, still changing after the command ended: agent-bg\.log' "$LAST_STDERR" "10 named on stderr"
has 'move_issue.*state-build-review' "$(calls)" "10 hand-off"
sleep 1
teardown

# 11 — an untracked symlink whose target the hook changes and commits
setup 'echo "added by the hook" >> a.txt; git add a.txt; git commit -qm "chore: fold in a.txt"'
export PR3_ON_CALL_1='ln -s a.txt link.txt'
pr3_run_implement
check_eq 0 "$LAST_RC" "11 exit"
has 'move_issue.*state-build-review' "$(calls)" "11 hand-off"
teardown

# 12 — the hook points that symlink elsewhere
setup 'rm -f link.txt; ln -s old.txt link.txt'
export PR3_ON_CALL_1='ln -s a.txt link.txt'
pr3_run_implement
check_eq 14 "$LAST_RC" "12 exit"
has 'changed further \(kept in the worktree\):.link\.txt' "$(calls | tr '\n' ' ')" "12 link named"
teardown

# 9 — negative control: without the content comparison the stage passes case 1, as v3.0.2 did
setup 'echo "added by the hook" >> a.txt'
export PR3_DIRTY="a.txt"
f="$SCRIPTS_DIR/implement-pipeline.sh"
grep -q 'if \[ -n "\$new_dirty" \] || \[ -n "\$changed_dirty" \]; then' "$f" || fail "9 control: the condition line was not found"
sed -i.bak 's/if \[ -n "\$new_dirty" \] || \[ -n "\$changed_dirty" \]; then/if [ -n "$new_dirty" ]; then/' "$f"
pr3_run_implement
check_eq 0 "$LAST_RC" "9 control: the old stage passes a hook that changed an uncommitted file"
has 'move_issue.*state-build-review' "$(calls)" "9 control: and hands it on"
teardown

trap - EXIT  # every case tore its own sandbox down
if [ "$FAILS" -gt 0 ]; then
  echo "test_post_implement_dirty: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_post_implement_dirty"
