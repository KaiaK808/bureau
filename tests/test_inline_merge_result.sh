#!/bin/bash
# The review stage acts on the result of its inline merge (agents.merge off, merge_mode
# auto). It used to call merge-pipeline.sh, which ended with 0 whether it merged or not,
# and to report "Next: Done" in every case: a PR whose checks had not started stayed in
# Build Review without needs-human, and the next poll paid a full three-specialist review.
#
# Runs the REAL code-review-pipeline.sh, merge-pipeline.sh, bureau-supervision.py and the
# real gate helpers in the harness sandbox; GitHub is the pr2 gh double, Linear the stub
# config, the models fake_claude.sh.
#   1. not yet (no check run on a fresh head): exit 2, no needs-human, no move, the
#      APPROVE is recorded for reuse, one ticket comment names the gate
#   2. the next run on the unchanged head: no model call, the gate runs again, no second
#      review comment on the PR and no second ticket comment
#   3. the checks turn green: the run after merges, the ticket goes to Done, no model call
#   4. blocked (a failing check): exit 25, needs-human, the gate line on the ticket, no record
#   5. the disposable worktree is back on the reviewed head and clean after 2, so the
#      worker does not keep it as unfinished work
#   6. the shepherd waits on the review stage's "not yet" as it does at Merge
# Negative control: the v3.0.2 inline call (and the merge stage's inline 0) end the first
# case with 0 and "Next: Done", and the second run pays the model again.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP
ISSUE=EXP-801
trap 'teardown || true' EXIT

fail() { echo "FAIL: $1" >&2; printf '%s\n--- stderr ---\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -60 >&2; exit 1; }

new_sandbox() {
  teardown || true
  sandbox_init "$ISSUE" test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  cat > "$SANDBOX/approve.txt" <<'EOF'
Review checked.
```json
{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"Checks passed"}
```
EOF
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt"
  export BUREAU_STUB_ISSUE_STATE='Build Review' BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  pr2_gate_setup
  STOPS="$(git -C "$SANDBOX" rev-parse --path-format=absolute --git-common-dir)/bureau/review-stops.json"
}
review() { rm -f "$SANDBOX/fake_claude_counter"; : > "$SANDBOX/gh_calls.log"; run_pipeline code-review-pipeline.sh "$ISSUE"; }
calls() { cat "$SANDBOX/calls.log" 2>/dev/null || true; }
labeled_human() { grep -q $'add_issue_label\t'"$ISSUE"$'\tneeds-human' "$SANDBOX/calls.log" 2>/dev/null; }
moved() { grep -q $'^move_issue\t' "$SANDBOX/calls.log" 2>/dev/null; }
record() { jq -c --arg i "$ISSUE" '.[$i] // empty' "$STOPS" 2>/dev/null || true; }
ticket_comments() { grep -c $'^post_comment\t' "$SANDBOX/calls.log" 2>/dev/null || true; }

# ── 1. not yet ─────────────────────────────────────────────────────────────
new_sandbox
pr2_checks none; pr2_head_age 60
review
[ "$LAST_RC" = 2 ] || fail "1: not yet ended $LAST_RC, wanted 2"
[ "$(pr2_model_calls)" -gt 0 ] || fail '1: the first review paid no model call'
pr2_merged && fail '1: merged a PR whose gate is not yet decided'
labeled_human && fail '1: set needs-human on a gate that is only not yet decided'
moved && fail '1: moved the ticket although nothing merged'
grep -q 'Next: Build Review (merge gate not yet decided' <<< "$LAST_STDOUT" || fail '1: the summary does not say the gate is not yet decided'
grep -q 'Next: Done' <<< "$LAST_STDOUT" && fail '1: still reports Done'
[ "$(record | jq -r '.verdict + " " + (.merge_gate_wait | tostring)')" = 'APPROVE true' ] || fail "1: no gate-wait approval recorded: $(record)"
[ "$(ticket_comments)" = 1 ] || fail "1: posted $(ticket_comments) ticket comments, wanted 1"
grep -q 'not merged yet: its merge gate is not decided' "$SANDBOX/calls.log" || fail '1: the ticket comment does not name the gate'
grep -q 'only 0 completed check' "$SANDBOX/calls.log" || fail '1: the ticket comment lacks the gate line'
[ "$(pr2_review_comments)" = 1 ] || fail "1: $(pr2_review_comments) review comments on the PR, wanted 1"
echo 'PASS 1 not yet: exit 2, no needs-human, no move, approval recorded, one ticket comment'

# ── 2. the next run on the unchanged head ──────────────────────────────────
: > "$SANDBOX/calls.log"
review
[ "$LAST_RC" = 2 ] || fail "2: the retry ended $LAST_RC, wanted 2"
[ "$(pr2_model_calls)" = 0 ] || fail "2: the retry made $(pr2_model_calls) model call(s), wanted none"
[ "$(pr2_gate_reads)" -ge 1 ] || fail '2: the retry did not run the gate again'
[ "$(pr2_review_comments)" = 1 ] || fail "2: the retry posted another review comment on the PR ($(pr2_review_comments))"
[ "$(ticket_comments)" = 0 ] || fail "2: the retry posted $(ticket_comments) ticket comment(s)"
labeled_human && fail '2: the retry set needs-human'
[ "$(record | jq -r .merge_gate_wait)" = true ] || fail '2: the retry did not record the approval again'
echo 'PASS 2 retry: no model call, gate read again, no new comments, approval recorded again'

# ── 3. green: merged, Done ─────────────────────────────────────────────────
pr2_checks green
: > "$SANDBOX/calls.log"
review
[ "$LAST_RC" = 0 ] || fail "3: the green run ended $LAST_RC, wanted 0"
[ "$(pr2_model_calls)" = 0 ] || fail '3: the green run paid a model call'
pr2_merged || fail '3: nothing merged on a green gate'
grep -q $'move_issue\t'"$ISSUE"$'\tstate-done' "$SANDBOX/calls.log" || fail '3: the ticket did not move to Done'
grep -q 'Next: Done' <<< "$LAST_STDOUT" || fail '3: the summary does not say Done'
[ -z "$(record)" ] || fail '3: the approval record survived the merge'
echo 'PASS 3 green: merged by the reused approval, Done, record consumed'

# ── 4. blocked ─────────────────────────────────────────────────────────────
new_sandbox
pr2_checks red
review
[ "$LAST_RC" = 25 ] || fail "4: blocked ended $LAST_RC, wanted 25"
pr2_merged && fail '4: merged on a red check'
labeled_human || fail '4: no needs-human on a blocked gate'
moved && fail '4: moved the ticket on a blocked gate'
grep -q 'merge gate is blocked' "$SANDBOX/calls.log" && grep -q 'failing check(s) on' "$SANDBOX/calls.log" \
  || fail '4: the ticket comment lacks the blocked gate line'
grep -q $'log_escalation\t'"$ISSUE"$'\tcode-review' "$SANDBOX/calls.log" || fail '4: no escalation record'
[ -z "$(record)" ] || fail '4: recorded an approval for a blocked gate'
echo 'PASS 4 blocked: exit 25, needs-human, gate line on the ticket, nothing recorded'

# ── 5. the worktree the worker finds after a gate outcome ──────────────────
# The build check leaves a file git does not ignore and a local commit (as the local
# validation merge does). bureau-worker.sh keeps a worktree that is dirty or ahead of
# origin after a non-zero exit as unfinished work and refuses to reset it, so the next
# gate run could not start. In a disposable worker the stage puts it back. The stage
# runs in a linked worktree here, as in a worker (the harness sandbox itself holds the
# scripts and the doubles as untracked files).
worktree_case() {  # <mode> [norelease] — runs the review in a fresh linked worktree; sets WT
  new_sandbox
  if [ "${2:-}" = norelease ]; then
    python3 - "$SCRIPTS_DIR/code-review-pipeline.sh" <<'PY'
import sys
p = sys.argv[1]; src = open(p).read()
old = '  [ "${BUREAU_WORKSPACE_MODE:-}" = disposable ] || return 0\n'
assert src.count(old) == 1, 'release guard not found'
open(p, 'w').write(src.replace(old, '  return 0\n'))
PY
  fi
  pr2_checks pending
  pr2_config '.repo.test_command = "echo out > leftover.txt && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m local-merge"'
  git -C "$SANDBOX" checkout -q --detach
  WT="$SANDBOX/.wt"
  git -C "$SANDBOX" worktree add -q --detach "$WT" origin/test-branch
  rm -f "$SANDBOX/fake_claude_counter"
  set +e
  ( cd "$WT" && BUREAU_WORKSPACE_MODE="$1" BUREAU_CONFIG="$SANDBOX/.bureau.json" \
      bash "$SCRIPTS_DIR/code-review-pipeline.sh" "$ISSUE" ) > "$SANDBOX/wt.out" 2> "$SANDBOX/wt.err"
  LAST_RC=$?
  set -e
  LAST_STDOUT=$(cat "$SANDBOX/wt.out"); LAST_STDERR=$(cat "$SANDBOX/wt.err")
}
worker_would_keep() {  # the two conditions bureau-worker.sh's cleanup checks after rc != 0
  [ -n "$(git -C "$WT" status --porcelain)" ] || [ "$(git -C "$WT" rev-list --count origin/test-branch..HEAD)" != 0 ]
}
worktree_case disposable
[ "$LAST_RC" = 2 ] || fail "5: ended $LAST_RC, wanted 2"
worker_would_keep && fail "5: the worker would keep the worktree: $(git -C "$WT" status --porcelain) ahead=$(git -C "$WT" rev-list --count origin/test-branch..HEAD)"
[ "$(git -C "$WT" rev-parse HEAD)" = "$(git -C "$SANDBOX" rev-parse origin/test-branch)" ] || fail '5: not on the reviewed head'
# Outside a disposable worker nothing is reset.
worktree_case current
[ "$LAST_RC" = 2 ] || fail "5: ended $LAST_RC outside a worker, wanted 2"
[ -e "$WT/leftover.txt" ] || fail '5: reset a checkout outside a disposable worker'
# Control: without the release the worker would keep the worktree and refuse the next reset.
worktree_case disposable norelease
[ "$LAST_RC" = 2 ] || fail "5 control: ended $LAST_RC, wanted 2"
worker_would_keep || fail '5 control: without the release the worktree should be kept, so this case proves nothing'
echo 'PASS 5 the disposable worktree is clean and on the reviewed head after exit 2'

# ── 6. the shepherd waits on the review stage's "not yet" ──────────────────
# The shepherd's real gate branch, cut from shepherd.sh, fed the report the review
# stage wrote for its caller.
new_sandbox
pr2_checks none
export BUREAU_MERGE_GATE_REPORT="$SANDBOX/caller-gate.report"
review
unset BUREAU_MERGE_GATE_REPORT
[ "$LAST_RC" = 2 ] || fail "6: ended $LAST_RC, wanted 2"
[ "$(head -n 1 "$SANDBOX/caller-gate.report")" = not-yet ] || fail '6: the caller got no not-yet report'
# The shepherd's real code, cut from shepherd.sh: the line that resets the wait before a
# stage and the gate branch after it, run as up to ten passes of its loop.
python3 - "$REPO_ROOT/templates/scripts/shepherd.sh" > "$SANDBOX/shepherd-gate.sh" <<'PY'
import sys
src = open(sys.argv[1]).read()
reset = [l for l in src.split('\n') if l.startswith('  ') and 'MERGE_WAITED=0' in l]
if len(reset) != 1: sys.exit('the wait reset line was not found once')
start = src.index("  # The merge stage's gate (see MERGE_WAIT_SECONDS above).")
end = src.index('  # Halt is the default for every code', start)
print('shepherd_passes() {\n for _pass in 1 2 3 4 5 6 7 8 9 10; do\n' + reset[0] + '\n' + src[start:end] + '  echo general; return\n done\n}')
PY
shepherd_passes() {  # <rc> <pipeline> — prints what the shepherd does with caller-gate.report
  RC="$1" PIPELINE="$2" MERGE_GATE_FILE="$SANDBOX/caller-gate.report" /bin/bash -c '
    source "$1"; MERGE_WAITED=0 MERGE_WAIT_FOR="" MERGE_WAIT_SECONDS=120 MERGE_POLL_SECONDS=60 STATE="Build Review"
    _shepherd_sleep() { echo "slept $1"; }
    _shepherd_merge_blocked() { echo "merge-halt $1"; exit 0; }
    shepherd_passes' _ "$SANDBOX/shepherd-gate.sh" 2>&1 || true
}
out=$(shepherd_passes 2 code-review-pipeline.sh)
[ "$(grep -c '^slept 60$' <<< "$out")" = 2 ] && grep -q 'merge-halt was still not eligible after 120s' <<< "$out" \
  || fail "6: the shepherd did not wait its budget on the review stage's not-yet and then halt: $out"
# A blocked review goes through the general handling (the stage already labeled and commented).
printf 'blocked\nci_green: ci: failing check(s) on x: ci\n' > "$SANDBOX/caller-gate.report"
out=$(shepherd_passes 25 code-review-pipeline.sh)
[ "$out" = general ] || fail "6: a blocked review did not take the general handling: $out"
# The merge stage's own blocked gate is unchanged.
out=$(shepherd_passes 25 merge-pipeline.sh)
grep -q 'merge-halt is blocked' <<< "$out" || fail "6: the merge stage's blocked gate changed: $out"
echo 'PASS 6 the shepherd waits its budget on the review stage not-yet; a blocked review halts through the general handling'

# ── Negative control: the v3.0.2 inline call ───────────────────────────────
new_sandbox
python3 - "$SCRIPTS_DIR/code-review-pipeline.sh" "$SCRIPTS_DIR/merge-pipeline.sh" <<'PY'
import re, sys
review, merge = sys.argv[1], sys.argv[2]
src = open(review).read()
start = src.index('      GATE_REPORT="$REVIEW_TMP/merge-gate.report"\n')
end = src.index('      esac\n    fi\n    ;;\n', start) + len('      esac\n')
src = src[:start] + '      BUREAU_INLINE_MERGE=1 bash "$SCRIPT_REPO/scripts/merge-pipeline.sh" "$ISSUE"\n      NEXT_STATE="Done"\n' + src[end:]
open(review, 'w').write(src)
src = open(merge).read()
old = '  echo "  Gate outcome: $outcome — exit $code"\n'
assert src.count(old) == 1, 'merge gate exit not found'
src = src.replace(old, '  if [ "${BUREAU_INLINE_MERGE:-0}" = 1 ]; then echo "  Gate outcome: $outcome (inline)"; exit 0; fi\n' + old)
open(merge, 'w').write(src)
PY
pr2_checks none
review
[ "$LAST_RC" = 0 ] && grep -q 'Next: Done' <<< "$LAST_STDOUT" && ! pr2_merged \
  || fail "negative control: the v3.0.2 inline call should end 0 with Done and nothing merged (rc $LAST_RC)"
review
[ "$(pr2_model_calls)" -gt 0 ] || fail 'negative control: the v3.0.2 retry should pay the model again'
echo 'PASS negative control: v3.0.2 reports Done for an unmerged PR and pays again on the next run'

echo 'OK test_inline_merge_result'
