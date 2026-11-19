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
#      review comment on the PR and no second ticket comment; a build that turns red on
#      such a run still posts its REQUEST_CHANGES on the PR (no stale APPROVE stays last)
#   3. the checks turn green: the run after merges, the ticket goes to Done, no model call
#   4. blocked (a failing check): exit 25, needs-human, the gate line on the ticket, the
#      APPROVE recorded: once the check is fixed and the label removed, the next run
#      merges without a model call; a push after the block means a new review
#   4b. a push that lands between the inline gate and the merge call: the pinned merge
#      refuses, the review ends 2, and the next run reviews the new head (a model call)
#   5. the disposable worktree is back on the reviewed head and clean after 2 and after
#      25, so the worker does not keep it as unfinished work
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
{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"missing_acceptance":[],"fixes_needed":[],"comment":"Review checked.","summary":"Checks passed"}
```
EOF
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt"
  review_clean_specialists
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

# 2b. A red build on a reused gate-wait approval folds it into REQUEST_CHANGES, and that
# verdict reaches the PR: the gate must not keep reading the old APPROVE.
new_sandbox
pr2_checks none
review
[ "$LAST_RC" = 2 ] || fail "2b: the first run ended $LAST_RC, wanted 2"
pr2_config '.repo.test_command = "exit 1"'
: > "$SANDBOX/calls.log"
review
[ "$LAST_RC" = 0 ] || fail "2b: the red-build retry ended $LAST_RC, wanted 0 (REQUEST_CHANGES)"
[ "$(pr2_model_calls)" = 0 ] || fail '2b: the red-build retry paid a model call'
[ "$(pr2_review_comments)" = 2 ] || fail "2b: $(pr2_review_comments) review comments on the PR, wanted 2 (the APPROVE and the REQUEST_CHANGES)"
last=$(jq -r '[.[] | select(.body | test("Code Review v2"))] | last | .body' "$PR2_GH/comments.json" | grep -oE '\*\*Verdict\*\*: [A-Z_]+' | sed -n 1p)
[ "$last" = '**Verdict**: REQUEST_CHANGES' ] || fail "2b: the latest review comment on the PR says '$last'"
grep -q $'move_issue\t'"$ISSUE"$'\tstate-build' "$SANDBOX/calls.log" || fail '2b: the ticket did not go back to Build'
pr2_merged && fail '2b: merged after a red build'
echo 'PASS 2b a red build on a reused approval posts REQUEST_CHANGES on the PR and goes back to Build'

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
[ "$(record | jq -r '.verdict + " " + ((.merge_gate_wait // false) | tostring)')" = 'APPROVE false' ] \
  || fail "4: no reusable approval recorded for the blocked gate: $(record)"
grep -q 'The approval is recorded for head' "$SANDBOX/calls.log" || fail '4: the ticket comment does not say the approval is kept'
# A human re-runs the check (now green) and removes needs-human (the stub's labels are the
# run-start labels, as in the record): the next run reuses the approval and merges.
pr2_checks green
: > "$SANDBOX/calls.log"
review
[ "$LAST_RC" = 0 ] && pr2_merged || fail "4: the run after the fix did not merge (rc $LAST_RC)"
[ "$(pr2_model_calls)" = 0 ] || fail "4: the run after the fix paid $(pr2_model_calls) model call(s)"
grep -q $'move_issue\t'"$ISSUE"$'\tstate-done' "$SANDBOX/calls.log" || fail '4: the ticket did not move to Done after the fix'
# A push after the block: the recorded approval does not cover the new head.
new_sandbox
pr2_checks red
review
[ "$LAST_RC" = 25 ] || fail "4: the second blocked run ended $LAST_RC"
bump=$(git -C "$SANDBOX" commit-tree "origin/test-branch^{tree}" -p origin/test-branch -m 'fix pushed')
git -C "$SANDBOX" push -q origin "$bump:refs/heads/test-branch"
jq --arg h "$bump" '.headRefOid = $h' "$PR2_GH/pr.json" > "$PR2_GH/pr.tmp" && mv "$PR2_GH/pr.tmp" "$PR2_GH/pr.json"
pr2_checks green
review
[ "$(pr2_model_calls)" -gt 0 ] || fail '4: a new head after the block reused the old approval'
echo 'PASS 4 blocked: exit 25, needs-human, gate line on the ticket, approval kept; reused after the fix, not after a push'

# ── 4b. the head moves between the inline gate and the merge ───────────────
new_sandbox
pr2_checks green
bump=$(git -C "$SANDBOX" commit-tree "origin/test-branch^{tree}" -p origin/test-branch -m 'pushed during the merge')
echo "$bump" > "$PR2_GH/move_head_on_merge"
review
[ "$LAST_RC" = 2 ] || fail "4b: a head that moved during the inline merge ended $LAST_RC, wanted 2"
pr2_merged && fail '4b: merged a head no gate had checked'
labeled_human && fail '4b: set needs-human for a moved head'
grep -q 'the PR head moved from' "$SANDBOX/calls.log" || fail '4b: the ticket comment does not name the moved head'
# The push reaches origin; the next run judges the new head with a new review.
git -C "$SANDBOX" push -q origin "$bump:refs/heads/test-branch"
rm -f "$PR2_GH/move_head_on_merge"
review
[ "$(pr2_model_calls)" -gt 0 ] || fail '4b: the new head was not reviewed again'
[ "$LAST_RC" = 0 ] && pr2_merged || fail "4b: the reviewed new head did not merge (rc $LAST_RC)"
echo 'PASS 4b a head that moves during the inline merge is not merged; the next run reviews it'

# ── 5. the worktree the worker finds after a gate outcome ──────────────────
# The build check leaves a file git does not ignore and a local commit (as the local
# validation merge does). bureau-worker.sh keeps a worktree that is dirty or ahead of
# origin after a non-zero exit as unfinished work and refuses to reset it, so the next
# gate run could not start. In a disposable worker the stage puts it back. The stage
# runs in a linked worktree here, as in a worker (the harness sandbox itself holds the
# scripts and the doubles as untracked files).
worktree_case() {  # <mode> [norelease] [checks] — runs the review in a fresh linked worktree; sets WT
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
  pr2_checks "${3:-pending}"
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
# The same after a blocked gate (25).
worktree_case disposable '' red
[ "$LAST_RC" = 25 ] || fail "5: the blocked case ended $LAST_RC, wanted 25"
worker_would_keep && fail "5: after 25 the worker would keep the worktree: $(git -C "$WT" status --porcelain) ahead=$(git -C "$WT" rev-list --count origin/test-branch..HEAD)"
# Outside a disposable worker nothing is reset.
worktree_case current
[ "$LAST_RC" = 2 ] || fail "5: ended $LAST_RC outside a worker, wanted 2"
[ -e "$WT/leftover.txt" ] || fail '5: reset a checkout outside a disposable worker'
# Control: without the release the worker would keep the worktree and refuse the next reset.
worktree_case disposable norelease
[ "$LAST_RC" = 2 ] || fail "5 control: ended $LAST_RC, wanted 2"
worker_would_keep || fail '5 control: without the release the worktree should be kept, so this case proves nothing'
echo 'PASS 5 the disposable worktree is clean and on the reviewed head after exit 2 and 25'

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

# A real review publishes approval of A; another normal push lands B before
# its inline merge. Both the review caller and a repeated inline gate return
# fresh-review work, without a human hold, approval reuse or duplicate comment.
inline_binding_case() {
  local control="$1" approved current
  new_sandbox
  [ "$control" != reverted ] || pr2_revert_approval_binding
  approved=$(jq -r .headRefOid "$PR2_GH/pr.json")
  printf 'source "%s"\npr2_push_head\n' "$LIB_DIR/pr2-gate.sh" > "$SANDBOX/push-head.sh"
  echo "$SANDBOX/push-head.sh" > "$PR2_GH/on_review_comment"
  export BUREAU_MERGE_GATE_REPORT="$SANDBOX/caller-gate.report"
  review
  unset BUREAU_MERGE_GATE_REPORT
  current=$(jq -r .headRefOid "$PR2_GH/pr.json")
  [ "$LAST_RC" = 20 ] && ! pr2_merged && ! labeled_human || return 1
  [ "$(head -n 1 "$SANDBOX/caller-gate.report")" = refreshed ] || return 1
  grep -qF "approved head $approved differs from current head $current" "$SANDBOX/caller-gate.report" || return 1
  grep -q $'move_issue\t'"$ISSUE"$'\tstate-build-review' "$SANDBOX/calls.log" || return 1
  [ -z "$(record)" ] || return 1
  [ "$(jq '[.[] | select(.body | test("Bureau merge gate"))] | length' "$PR2_GH/comments.json")" = 1 ] || return 1
  BUREAU_INLINE_MERGE=1 run_pipeline merge-pipeline.sh "$ISSUE"
  [ "$LAST_RC" = 20 ] && ! pr2_merged && ! labeled_human || return 1
  [ "$(jq '[.[] | select(.body | test("Bureau merge gate"))] | length' "$PR2_GH/comments.json")" = 1 ] || return 1
}
inline_binding_case current || fail "inline binding: rc $LAST_RC"
if inline_binding_case reverted; then fail 'inline binding: negative control stayed green'; fi
[ "$LAST_RC" = 0 ] && pr2_merged || fail 'inline binding: negative control failed before the unsafe merge'
echo 'PASS inline binding: real review of A followed by pushed B returns 20/refreshed once; identical assertions reject reverted binding'

# A real review approves main. After both gate passes, the target moves while
# the merge message is prepared. The approved head remains the same.
inline_retarget_case() {
  local control="$1"
  new_sandbox
  [ "$control" = current ] || pr2_revert_merge_base_fix target
  pr2_retarget_after_read merge-message 1
  export BUREAU_MERGE_GATE_REPORT="$SANDBOX/caller-gate.report"
  review
  unset BUREAU_MERGE_GATE_REPORT
  [ "$LAST_RC" = 20 ] && ! pr2_merged && ! labeled_human || return 1
  [ "$(head -n 1 "$SANDBOX/caller-gate.report")" = refreshed ] || return 1
  grep -q '^approval_binding: approved Target branch main differs from current base branch release' "$SANDBOX/caller-gate.report" || return 1
  grep -q $'move_issue\t'"$ISSUE"$'\tstate-build-review' "$SANDBOX/calls.log" || return 1
  ! grep -q $'^gh\tpr\tmerge\t' "$SANDBOX/gh_calls.log" || return 1
  [ -z "$(record)" ] || return 1
  [ "$(jq '[.[] | select(.body | test("Bureau merge gate"))] | length' "$PR2_GH/comments.json")" = 1 ]
}
inline_retarget_case current || fail "inline retarget after final gate: rc $LAST_RC"
if inline_retarget_case reverted; then fail 'inline retarget after final gate: negative control stayed green'; fi
[ "$LAST_RC" = 0 ] && pr2_merged && [ "$(jq -r .baseRefName "$PR2_GH/pr.json")" = release ] \
  || fail 'inline retarget after final gate: negative control failed before the unsafe merge'
echo 'PASS inline retarget after final gate: 20/refreshed, one gate comment, no needs-human; private reverted copy merges into release'

# The real writer and gate must agree even when the target contains a backtick.
target_round_trip() {
  local target="$1" base body
  new_sandbox
  git -C "$SANDBOX" push -q origin "origin/main:refs/heads/$target" || fail "invalid fixture target $target"
  jq --arg ref "$target" '.baseRefName = $ref' "$PR2_GH/pr.json" > "$PR2_GH/pr.tmp" && mv "$PR2_GH/pr.tmp" "$PR2_GH/pr.json"
  if [ "${2:-}" = reverted ]; then
    python3 - "$SCRIPTS_DIR/merge-pipeline.sh" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
source = path.read_text()
pattern = '([^ ]+)` at'
assert source.count(pattern) == 1
path.write_text(source.replace(pattern, '([^`]+)` at'))
PY
  fi
  review
  base=$(git -C "$SANDBOX" rev-parse origin/main)
  body=$(jq -er '.[0].body' "$PR2_GH/comments.json") || fail "writer did not publish a review for $target"
  grep -qxF "**Target**: \`$target\` at \`$base\`" <<< "$body" \
    || fail "writer did not publish the exact target $target"
  [ "$LAST_RC" = 0 ] && pr2_merged
}
for target in 'release`qa' 'release/v2.1'; do
  target_round_trip "$target" || fail "target $target did not round-trip (rc $LAST_RC)"
done
if target_round_trip 'release`qa' reverted; then fail 'backtick target: negative control stayed green'; fi
[ "$LAST_RC" = 20 ] && ! pr2_merged && grep -q 'Target line missing, malformed or duplicated' <<< "$LAST_STDOUT" \
  || fail 'backtick target: the old parser did not reject the real writer output as malformed'
echo 'PASS real Target lines with a backtick, slash and dot merge; private reverted parser rejects the backtick branch'

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

# Exercise the real worker lease, reset and EXIT cleanup. The only doubles are
# the service transports and the reviewer. All pushes go to a private bare origin.
source "$LIB_DIR/pr5-interrupt.sh"
review_worker_fixture() {
  pr5_setup
  trap 'rm -rf "$SB"' EXIT
  export GIT_CEILING_DIRECTORIES="$SB"
  pr5_new_repo
  export SANDBOX="$REPO" BUREAU_STUB_BRANCH=test-branch
  SCRIPTS_DIR="$REPO/scripts"
  git -C "$REPO" checkout -qb test-branch
  printf 'feature\n' > "$REPO/feature.txt"
  git -C "$REPO" add feature.txt
  git -C "$REPO" commit -qm 'review fixture feature'
  git -C "$REPO" push -q origin test-branch
  git -C "$REPO" checkout -q main
  jq --argjson merge "$1" --argjson rounds "$2" '
    .agents += {code_review:true, merge:$merge, max_review_rounds:$rounds}
    | .repo.test_command = "touch leftover.txt"' "$REPO/.bureau.json" > "$SB/config"
  mv "$SB/config" "$REPO/.bureau.json"
  printf s6 > "$SB/state"
  jq -nc --arg number "${ISSUE##*-}" '{issue:$number, body:"<!-- bureau-branch: test-branch -->"}' > "$SB/comments.jsonl"
  pr2_gate_setup

  cat > "$SB/bin/claude" <<'PROVIDER'
#!/bin/bash
case "$1" in
  auth) echo '{"loggedIn":true}'; exit 0 ;;
  --help) echo '--json-schema --disallowedTools'; exit 0 ;;
  --version) echo '2.0.0'; exit 0 ;;
esac
case "$BUREAU_PROVIDER_PHASE" in
  correctness|security|performance)
    jq -nc --arg role "$BUREAU_PROVIDER_PHASE" '{specialist:$role, counts:{critical:0,bug:0,minor:0,skip:0}, findings:[], summary:"Clean."}' ;;
  verdict) echo '{"verdict":"APPROVE","bugs":0,"security_issues":0,"missing_acceptance":[],"fixes_needed":[],"findings":[],"comment":"Clean.","summary":"Clean."}' ;;
  *) exit 1 ;;
esac | jq -c '{structured_output:.,result:tojson}'
PROVIDER
  chmod +x "$SB/bin/claude"

  cat > "$SB/on-review" <<'HOOK'
set -euo pipefail
cd "$SANDBOX"
origin=$(git remote get-url origin)
ref=refs/heads/test-branch
head=$(git --git-dir "$origin" rev-parse "$ref")
base=$(git --git-dir "$origin" rev-parse refs/heads/main)
if [ -f "$PR2_GH/force-push" ]; then
  head=$(git commit-tree "$head^{tree}" -p "$base" -m 'rewrite after approval')
  git push -q --force origin "$head:$ref"
else
  reviews=$(jq '[.[] | select(.body | startswith("## Code Review v2"))] | length' "$PR2_GH/comments.json")
  case "$reviews" in
    1|2)
      head=$(git commit-tree "$head^{tree}" -p "$head" -m 'advance after approval')
      git push -q origin "$head:$ref" ;;
    3)
      base=$(git commit-tree "$base^{tree}" -p "$base" -m 'sibling lands after approval')
      git push -q origin "$base:refs/heads/main" ;;
  esac
  [ "$reviews" -gt 3 ] || printf '%s\n' "$0" > "$PR2_GH/on_review_comment"
fi
pr_base=$(git --git-dir "$origin" merge-base "$base" "$head")
jq --arg h "$head" --arg b "$pr_base" '.headRefOid = $h | .baseRefOid = $b' "$PR2_GH/pr.json" > "$PR2_GH/pr.tmp"
mv "$PR2_GH/pr.tmp" "$PR2_GH/pr.json"
jq -n --arg base "$base" '{commit:{sha:$base}}' > "$PR2_GH/branch.json"
HOOK
  printf '%s\n' "$SB/on-review" > "$PR2_GH/on_review_comment"
}

review_worker_run() {
  LAST_RC=0
  (cd "$REPO" && _pr5_env "$@") > "$SB/out" 2> "$SB/err" || LAST_RC=$?
  LAST_STDOUT=$(cat "$SB/out"); LAST_STDERR=$(cat "$SB/err")
}

force_push_worker_case() (
  review_worker_fixture false 4
  touch "$PR2_GH/force-push"
  local approved replacement first_rc status branch newest registered=false
  approved=$(jq -r .headRefOid "$PR2_GH/pr.json")
  if [ "$1" = reverted ]; then
    sed 's/_review_release_worktree stale-approval/_review_release_worktree/' "$SCRIPTS_DIR/code-review-pipeline.sh" > "$SB/review"
    cp "$SB/review" "$SCRIPTS_DIR/code-review-pipeline.sh"
  fi
  review_worker_run env BUREAU_MERGE_GATE_REPORT="$SB/gate.report" bash scripts/bureau-worker.sh "$ISSUE" code-review-pipeline.sh "$WT" test-branch
  first_rc=$LAST_RC
  replacement=$(jq -r .headRefOid "$PR2_GH/pr.json")
  git -C "$REPO" merge-base --is-ancestor "$approved" "$replacement" && fail 'force-push fixture did not rewrite the approved commit'
  grep -qF "approved head $approved differs from current head $replacement" "$SB/gate.report" \
    && [ "$(head -n 1 "$SB/gate.report")" = refreshed ] && [ "$(cat "$SB/state")" = s6 ] \
    && ! pr2_merged || fail "force-push did not reach stale approval (rc $first_rc)"
  status=$(git -C "$WT" status --porcelain)
  branch=$(git -C "$WT" branch --show-current)
  [ ! -f "$COMMON/bureau/workers/$(printf '%s' "$WT" | shasum -a 256 | cut -d' ' -f1)" ] || registered=true
  review_worker_run bash scripts/bureau-worker.sh "$ISSUE" code-review-pipeline.sh "$WT" test-branch
  if [ "$1" = reverted ]; then
    [ "$first_rc" = 1 ] && [ "$LAST_RC" = 21 ] && [ "$registered" = false ] \
      && grep -q 'needs-human' "$SB/linear.log" && [ "$(pr2_review_comments)" = 1 ] \
      || fail "force-push control: expected checkpoint failure then ownership halt, got $first_rc then $LAST_RC"
  else
    [ "$first_rc" = 20 ] && [ -z "$status" ] && [ -z "$branch" ] && [ "$registered" = true ] \
      && [ "$LAST_RC" = 0 ] && [ "$(pr2_review_comments)" = 2 ] && pr2_merged \
      && ! grep -q 'needs-human' "$SB/linear.log" || fail "force-push handoff: worker returned $first_rc then $LAST_RC"
    newest=$(jq -er '[.[] | select(.body | startswith("## Code Review v2"))] | last | .body' "$PR2_GH/comments.json")
    grep -qxF "**Reviewed head**: \`$replacement\`" <<< "$newest" || fail 'the next worker did not review the replacement commit'
  fi
)
force_push_worker_case current
force_push_worker_case reverted
echo 'PASS force-pushed inline approval: worker returns 20, detaches cleanly, then reviews the new head; old reset returns 1 then 21'

shepherd_refresh_case() (
  review_worker_fixture true "$2"
  # Supply approvals at the review-stage boundary. The real shepherd, worker,
  # merge gate and merge-forward transaction process every handoff below.
  cat > "$SCRIPTS_DIR/code-review-pipeline.sh" <<'REVIEW'
#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
bureau_load_env --export "$BUREAU_ENV_FILE"
head=$(git rev-parse HEAD)
base=$(git rev-parse origin/main)
gh pr comment 99 --body "$(printf '## Code Review v2\n**Verdict**: APPROVE\n**Reviewed head**: `%s`\n**Target**: `main` at `%s`\n' "$head" "$base")"
move_issue "$1" "$BUREAU_STATE_MERGE"
REVIEW
  case "$1" in
    reverted)
      sed 's/if grep -q .\^base_refresh: .*"\$MERGE_GATE_FILE"; then/if true; then/' "$SCRIPTS_DIR/shepherd.sh" > "$SB/shepherd"
      cp "$SB/shepherd" "$SCRIPTS_DIR/shepherd.sh" ;;
    unbounded)
      sed 's/_shepherd_merge_blocked "kept receiving stale approvals .*"/:/' "$SCRIPTS_DIR/shepherd.sh" > "$SB/shepherd"
      cp "$SB/shepherd" "$SCRIPTS_DIR/shepherd.sh" ;;
  esac
  review_worker_run env BUREAU_DISPATCH_LANE=1 bash scripts/shepherd.sh --no-tmux --respect-config --worktree "$WT" "$ISSUE"
  [ "$(grep -c '^  Stale approval — fresh Build Review required' <<< "$LAST_STDOUT")" = 2 ] \
    || fail "shepherd refresh: expected two stale approvals (rc $LAST_RC)"
  local origin head base
  origin=$(git -C "$REPO" remote get-url origin)
  head=$(git --git-dir "$origin" rev-parse test-branch)
  base=$(git --git-dir "$origin" rev-parse main)
  case "$1" in
    reverted)
      [ "$LAST_RC" = 25 ] && [ "$(pr2_review_comments)" = 3 ] && ! pr2_merged \
        && grep -q 'needs-human' "$SB/linear.log" && ! git -C "$REPO" merge-base --is-ancestor "$base" "$head" \
        || fail 'shepherd refresh control: expected a hold with base advancement still missing from the head' ;;
    capped)
      [ "$LAST_RC" = 25 ] && [ "$(pr2_review_comments)" = 2 ] && ! pr2_merged \
        && grep -q 'kept receiving stale approvals after 1 fresh-review handoffs' <<< "$LAST_STDERR" \
        && grep -q 'needs-human' "$SB/linear.log" || fail 'shepherd stale-approval limit did not stop the next handoff' ;;
    *)
      [ "$LAST_RC" = 0 ] && [ "$(pr2_review_comments)" = 4 ] && pr2_merged \
        && grep -q '^  Base refreshed — fresh Build Review required' <<< "$LAST_STDOUT" \
        && ! grep -q 'needs-human' "$SB/linear.log" && git -C "$REPO" merge-base --is-ancestor "$base" "$head" \
        || fail 'shepherd did not merge-forward after two stale approvals' ;;
  esac
)
shepherd_refresh_case current 4
shepherd_refresh_case reverted 4
shepherd_refresh_case capped 1
shepherd_refresh_case unbounded 1
echo 'PASS shepherd: two stale approvals leave a base refresh available; the separate stale handoff limit stops repeated approvals'

echo 'OK test_inline_merge_result'
