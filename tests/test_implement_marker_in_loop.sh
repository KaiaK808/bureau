#!/bin/bash
# The squash-range check inside the implement loop, on the real implement stage
# (tests/lib/harness.sh: stubbed Linear/gh/model, real git with a bare origin, the real
# check_squash_range and squash-marker-check.sh). A CI suppressor in a commit message stops
# the loop after the iteration that wrote it; the check before the hand-off stays.
#
#   1  marker in iteration 1's commit, three iterations allowed → one provider call,
#      CI_MARKER, draft PR, needs-human, no hand-off, the hook does not run, no push after
#      the iteration, and the end-of-run push still puts the work on origin
#  1b  the range cannot be read (the marker list is missing) → one call, CI_MARKER
#  1c  a read failure only in the in-loop check (the final check reads a clean range) →
#      the in-loop report is on stderr and in the summary's iteration log
#   2  marker in iteration 2 → two calls; iteration 1 was pushed, iteration 2 was not
#      pushed after the iteration, both are on origin at the end; the summary names iter 2
#   3  a marker already on the branch before the run, clean iterations → stops after one
#   4  clean iterations → all three run, no in-loop stop (no false positive)
#   5  the /goal path (no iterations) keeps its single check after the run
#   6  negative control: the stage without the in-loop check makes three calls for case 1,
#      which is what v3.0.2 did
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
provider_calls() { cat "$SANDBOX/fake_claude_counter" 2>/dev/null || echo 0; }
origin_subjects() { git -C "$SANDBOX/.fake-origin.git" log --format=%s test-branch 2>/dev/null || true; }

MARK_DIR=$(mktemp -d -t bureau-pr3-marker.XXXXXX)

setup() {  # setup <case> — three iterations allowed, PARTIAL with progress, pushes counted
  sandbox_init "EXP-100" "test-branch"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt"
  export BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
  unset BUREAU_USE_GOAL_LOOP FAKE_CLAUDE_COMMIT_MSG FAKE_CLAUDE_COMMIT_ON_ITERS PR3_MARKER_ON PR3_MARKER_MSG PR3_DIRTY PR3_RENAME GH_STUB_EXISTING_PR
  export MARK="$MARK_DIR/$1"
  rm -f "$MARK"
  # A hook that would run for a PARTIAL with commits: it must not run for a halted loop.
  jq -n '{repo: {post_implement_command: "echo ran >> \"$MARK\""}}' > "$SANDBOX/.bureau.json"
  pr3_count_pushes
  pr3_count_receives
}

# 1 — marker in iteration 1
setup c1
export FAKE_CLAUDE_COMMIT_ON_ITERS="1" FAKE_CLAUDE_COMMIT_MSG="EXP-100: add the thing [skip ci]"
pr3_run_implement
check_eq 0 "$LAST_RC" "1 exit (a halt, labelled)"
check_eq 1 "$(provider_calls)" "1 one provider call"
has 'terminal status=CI_MARKER \(after 1 iter' "$LAST_STDOUT" "1 CI_MARKER after one iteration"
has 'squash-range check after iter 1 is not clean' "$LAST_STDERR" "1 in-loop stop is loud"
has $'^gh\tpr\tcreate\t--draft' "$(gh_log)" "1 draft PR"
hasnt $'^gh\tpr\tready' "$(gh_log)" "1 PR not marked ready"
has 'add_issue_label.*needs-human' "$(calls)" "1 needs-human"
hasnt 'move_issue' "$(calls)" "1 no hand-off"
has 'iter 1: squash-range check not clean, loop stopped' "$(calls)" "1 summary names the iteration"
has 'Squash-range check:' "$(calls)" "1 summary carries the final report"
[ ! -e "$MARK" ] || fail "1 the post-implement hook ran for a halted loop"
has 'post_implement_command: skipped \(status CI_MARKER' "$LAST_STDOUT" "1 hook skipped"
check_eq 1 "$(pr3_pushes)" "1 no push after the iteration, one at the end"
has 'add the thing \[skip ci\]' "$(origin_subjects)" "1 the work is on origin"
teardown

# 1b — a range the check cannot read (its list is missing) stops the loop the same way
setup c1b
export FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3"
rm "$SCRIPTS_DIR/ci-skip-markers.txt"
pr3_run_implement
check_eq 1 "$(provider_calls)" "1b one provider call"
has 'terminal status=CI_MARKER \(after 1 iter' "$LAST_STDOUT" "1b CI_MARKER after one iteration"
has 'could not be checked \(exit code 2\)' "$(calls)" "1b summary says the range was not checked"
hasnt 'move_issue' "$(calls)" "1b no hand-off"
teardown

# 1c — the in-loop check cannot read the range once; the final check reads it as clean
setup c1c
export FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3"
mv "$SCRIPTS_DIR/squash-marker-check.sh" "$SCRIPTS_DIR/squash-marker-check.real.sh"
cat > "$SCRIPTS_DIR/squash-marker-check.sh" <<EOF
#!/bin/bash
if [ ! -e "$SANDBOX/.pr3-transient-done" ]; then
  : > "$SANDBOX/.pr3-transient-done"
  echo "squash-marker-check: NOT CHECKED — listing origin/main..HEAD failed (exit code 128)"
  exit 2
fi
exec bash "$SCRIPTS_DIR/squash-marker-check.real.sh" "\$@"
EOF
pr3_run_implement
check_eq 1 "$(provider_calls)" "1c one provider call"
has 'terminal status=CI_MARKER' "$LAST_STDOUT" "1c CI_MARKER"
has 'NOT CHECKED — listing origin/main\.\.HEAD failed' "$LAST_STDERR" "1c the in-loop report is on stderr"
has 'iter 1: squash-range check not clean, loop stopped:.*NOT CHECKED — listing origin/main\.\.HEAD failed' "$(calls | tr '\n' ' ')" "1c and in the summary's iteration log"
teardown

# 2 — marker in iteration 2: iteration 1 ran and was pushed as usual
setup c2
pr3_fake_claude
export FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3" PR3_MARKER_ON=2 PR3_MARKER_MSG="EXP-100: tidy up [ci skip]"
pr3_run_implement
check_eq 2 "$(provider_calls)" "2 two provider calls"
has 'terminal status=CI_MARKER \(after 2 iter' "$LAST_STDOUT" "2 CI_MARKER after two iterations"
has 'iter 2: squash-range check not clean, loop stopped' "$(calls)" "2 summary names iter 2"
check_eq 2 "$(pr3_pushes)" "2 iteration 1 pushed, iteration 2 only at the end"
check_eq 2 "$(pr3_receives)" "2 origin received twice"
has 'tidy up \[ci skip\]' "$(origin_subjects)" "2 iteration 2's commit on origin"
has 'fake-claude iter 1 progress' "$(origin_subjects)" "2 iteration 1's commit on origin"
teardown

# 3 — a marker already on the branch (an earlier run's commit, not reworded)
setup c3
printf 'x\n' > "$SANDBOX/earlier.txt"
git -C "$SANDBOX" add earlier.txt
git -C "$SANDBOX" commit -qm "EXP-100: earlier work [no ci]"
git -C "$SANDBOX" push -q origin test-branch
export FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3"
pr3_run_implement
check_eq 1 "$(provider_calls)" "3 one provider call"
has 'terminal status=CI_MARKER' "$LAST_STDOUT" "3 CI_MARKER"
teardown

# 3b — the same with an iteration that commits nothing (the check does not wait for a commit)
setup c3b
printf 'x\n' > "$SANDBOX/earlier.txt"
git -C "$SANDBOX" add earlier.txt
git -C "$SANDBOX" commit -qm "EXP-100: earlier work [no ci]"
git -C "$SANDBOX" push -q origin test-branch
pr3_run_implement
check_eq 1 "$(provider_calls)" "3b one provider call"
has 'terminal status=CI_MARKER' "$LAST_STDOUT" "3b CI_MARKER"
teardown

# 4 — clean iterations: no in-loop stop
setup c4
export FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3"
pr3_run_implement
check_eq 0 "$LAST_RC" "4 exit"
check_eq 3 "$(provider_calls)" "4 three provider calls"
has 'terminal status=PARTIAL' "$LAST_STDOUT" "4 PARTIAL"
hasnt 'squash-range check after iter' "$LAST_STDERR" "4 no in-loop stop"
check_eq 3 "$(pr3_receives)" "4 each iteration reached origin"
teardown

# 5 — /goal path: one call, the check after the run halts it
setup c5
export BUREAU_USE_GOAL_LOOP=1 FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
export FAKE_CLAUDE_COMMIT_ON_ITERS="1" FAKE_CLAUDE_COMMIT_MSG="EXP-100: add the thing [skip actions]"
pr3_run_implement
check_eq 1 "$(provider_calls)" "5 one provider call"
has 'terminal status=CI_MARKER' "$LAST_STDOUT" "5 CI_MARKER"
hasnt 'move_issue' "$(calls)" "5 no hand-off"
teardown

# 6 — negative control: without the in-loop check, case 1 pays for all three iterations
setup c6
export FAKE_CLAUDE_COMMIT_ON_ITERS="1" FAKE_CLAUDE_COMMIT_MSG="EXP-100: add the thing [skip ci]"
f="$SCRIPTS_DIR/implement-pipeline.sh"
python3 - "$f" <<'PY' || fail "6 control: the in-loop block was not found"
import sys
p = sys.argv[1]
s = open(p).read()
start = s.index('  check_squash_range origin/main\n  if [ "$SQUASH_CHECK" != "clean" ]; then\n')
end = s.index('    break\n  fi\n', start) + len('    break\n  fi\n')
open(p, 'w').write(s[:start] + s[end:])
PY
pr3_run_implement
check_eq 3 "$(provider_calls)" "6 control: the stage without the check makes three calls"
has 'terminal status=CI_MARKER' "$LAST_STDOUT" "6 control: and halts only after the loop"
teardown

rm -rf "$MARK_DIR"
trap - EXIT  # every case tore its own sandbox down
if [ "$FAILS" -gt 0 ]; then
  echo "test_implement_marker_in_loop: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_implement_marker_in_loop"
