#!/bin/bash
# A review that stopped before merge (--no-merge) recorded its APPROVE. Run again without
# a stop on the same PR, head, base and ticket, the review stage reuses that approval: no
# model call, the build check still runs, the ticket moves on as after any APPROVE, and
# the record is gone afterwards. Every difference means the full, paid review.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP
ISSUE=EXP-701
sandbox_init "$ISSUE" test-branch
MARK_DIR=$(mktemp -d -t bureau-reuse-mark.XXXXXXXX)
trap 'teardown; rm -rf "$MARK_DIR"' EXIT

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
# The build check writes its marker outside the worktree, so the worktree stays clean.
jq -n --arg cmd "echo built >> '$MARK_DIR/build.log'; exit \"\${REUSE_BUILD_RC:-0}\"" \
  '{repo:{test_command:$cmd}}' > "$SANDBOX/.bureau.json"
git -C "$SANDBOX" add .bureau.json && git -C "$SANDBOX" commit -q -m 'config' && git -C "$SANDBOX" push -q origin test-branch
export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt"
export BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0 REUSE_BUILD_RC=0

STOPS="$(git -C "$SANDBOX" rev-parse --path-format=absolute --git-common-dir)/bureau/review-stops.json"
model_calls() { cat "$SANDBOX/fake_claude_counter" 2>/dev/null || echo 0; }
builds() { grep -c built "$MARK_DIR/build.log" 2>/dev/null || true; }
reset_logs() { rm -f "$SANDBOX/fake_claude_counter" "$SANDBOX/calls.log" "$SANDBOX/gh_calls.log"; : > "$MARK_DIR/build.log"; }
record_present() { [ -f "$STOPS" ] && jq -e --arg i "$ISSUE" 'has($i)' "$STOPS" >/dev/null 2>&1; }
fail() { echo "FAIL: $1" >&2; printf '%s\n%s\n' "$LAST_STDOUT" "$LAST_STDERR" >&2; exit 1; }

# A --no-merge review: paid, APPROVE, record written, exit 20.
stop_review() {
  python3 "$SCRIPTS_DIR/bureau-supervision.py" --repo "$SANDBOX" resume "$ISSUE" >/dev/null
  reset_logs
  export BUREAU_NO_MERGE=1
  run_pipeline code-review-pipeline.sh "$ISSUE"
  export BUREAU_NO_MERGE=0
  [ "$LAST_RC" = 20 ] || fail "stop review ended $LAST_RC, wanted 20"
  [ "$(model_calls)" -gt 0 ] || fail 'the stop review made no model call'
  record_present || fail 'the stop review recorded no approval'
  jq -e --arg i "$ISSUE" '.[$i].verdict == "APPROVE"' "$STOPS" >/dev/null || fail 'the record carries no APPROVE verdict'
  reset_logs
}
resumed_review() { run_pipeline code-review-pipeline.sh "$ISSUE"; }
expect_paid() {
  [ "$(model_calls)" -gt 0 ] || fail "$1: no model call, wanted a full review"
  record_present && fail "$1: the record survived a run that could not reuse it"
  [ "$LAST_RC" = 0 ] || fail "$1: ended $LAST_RC, wanted 0 (fresh APPROVE)"
  return 0
}

# 1. Same PR, head, base and ticket: reused, no model call, build check ran, moved to Merge.
stop_review
resumed_review
[ "$LAST_RC" = 0 ] || fail "reuse ended $LAST_RC, wanted 0"
[ "$(model_calls)" = 0 ] || fail "reuse made $(model_calls) model call(s), wanted none"
[ "$(builds)" = 1 ] || fail "reuse ran the build check $(builds) time(s), wanted 1"
grep -q $'move_issue\t'"$ISSUE"$'\tstate-merge' "$SANDBOX/calls.log" || fail 'reuse did not move the ticket to Merge'
grep -q 'reusing the approval recorded' "$SANDBOX/calls.log" || fail 'reuse posted no ticket comment'
record_present && fail 'the reused record was not consumed'
echo 'PASS 1 reuse: no model call, build check ran, moved to Merge, record consumed'

# 2. Consumed: the next run on the same inputs pays again.
reset_logs; resumed_review; expect_paid '2 consumed record'
echo 'PASS 2 a consumed record is not reused twice'

# 3. Each changed input means the full review, and the record is removed.
stop_review
bump=$(git -C "$SANDBOX" commit-tree "origin/test-branch^{tree}" -p origin/test-branch -m 'new head')
git -C "$SANDBOX" push -q origin "$bump:refs/heads/test-branch"
resumed_review; expect_paid '3a new head'
stop_review
bump=$(git -C "$SANDBOX" commit-tree "origin/main^{tree}" -p origin/main -m 'new base')
git -C "$SANDBOX" push -q origin "$bump:refs/heads/main"
resumed_review; expect_paid '3b moved base'
stop_review
export BUREAU_STUB_LABELS='["reopened"]'; resumed_review; expect_paid '3c edited ticket'
unset BUREAU_STUB_LABELS
stop_review
export GH_STUB_EXISTING_PR=100; resumed_review; expect_paid '3d other PR'
export GH_STUB_EXISTING_PR=99
stop_review
jq --arg i "$ISSUE" 'del(.[$i].verdict)' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
resumed_review; expect_paid '3e record without a verdict (written before verdicts were recorded)'
stop_review
jq --arg i "$ISSUE" '.[$i].verdict = "BLOCK"' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
resumed_review; expect_paid '3f recorded verdict not APPROVE'
stop_review
jq --arg i "$ISSUE" '.[$i].state = "Build"' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
resumed_review; expect_paid '3g other state'
stop_review
jq --arg i "$ISSUE" '.[$i].base_ref = "release"' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
resumed_review; expect_paid '3h other base branch'
stop_review
jq --arg i "$ISSUE" '.[$i].branch = "other-branch"' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
resumed_review; expect_paid '3i other branch'
echo 'PASS 3 head, base, ticket, PR, verdict, state, base branch and branch each force a full review'

# 4. No record, and an unreadable record file: full review, the file is left for a human.
python3 "$SCRIPTS_DIR/bureau-supervision.py" --repo "$SANDBOX" resume "$ISSUE" >/dev/null
reset_logs; resumed_review; expect_paid '4a no record'
printf 'not json\n' > "$STOPS"
reset_logs; run_pipeline code-review-pipeline.sh "$ISSUE"
[ "$(model_calls)" -gt 0 ] || fail '4b an unreadable record file was not answered with a full review'
printf '%s' "$LAST_STDERR" | grep -q 'review boundary file could not be checked' || fail '4b no warning for an unreadable record file'
[ "$(cat "$STOPS")" = 'not json' ] || fail '4b the unreadable record file was changed'
rm -f "$STOPS"
# 4c. A ticket detail without a label list cannot be fingerprinted: full review, the
# record is removed all the same, and it is a plain "no", not a file warning.
stop_review
export BUREAU_STUB_LABELS=null; resumed_review; unset BUREAU_STUB_LABELS
expect_paid '4c ticket detail without a label list'
printf '%s' "$LAST_STDOUT" | grep -q 'No reusable approval: ticket detail unreadable' || fail '4c the reason does not name the ticket detail'
printf '%s' "$LAST_STDERR" | grep -q 'could not be checked' && fail '4c an unreadable ticket detail was reported as a file problem'
echo 'PASS 4 no record, an unreadable record file or an unreadable ticket detail: full review'

# 5. A stop that is still requested behaves as before: exit 20, no model call, record kept.
stop_review
export BUREAU_NO_MERGE=1; resumed_review; export BUREAU_NO_MERGE=0
[ "$LAST_RC" = 20 ] || fail "a still-requested stop ended $LAST_RC, wanted 20"
[ "$(model_calls)" = 0 ] || fail 'a still-requested stop made a model call'
record_present || fail 'a still-requested stop removed the record'
[ "$(builds)" = 0 ] || fail 'a still-requested stop ran the build check'
echo 'PASS 5 a still-requested stop is unchanged'

# 6. A red build folds the reused APPROVE like a fresh one: REQUEST_CHANGES, back to Build.
reset_logs
export REUSE_BUILD_RC=1; resumed_review; export REUSE_BUILD_RC=0
[ "$(model_calls)" = 0 ] || fail '6 a red build under reuse made a model call'
[ "$(builds)" = 1 ] || fail '6 the build check did not run'
grep -q $'move_issue\t'"$ISSUE"$'\tstate-build$' "$SANDBOX/calls.log" || fail '6 a red build did not send the ticket back to Build'
grep -q $'move_issue\t'"$ISSUE"$'\tstate-merge' "$SANDBOX/calls.log" && fail '6 a red build still moved to Merge'
record_present && fail '6 the record survived'
echo 'PASS 6 a red build folds the reused approval into REQUEST_CHANGES'

# 7. A dry run neither reuses nor consumes the record.
stop_review
export BUREAU_DRY_RUN=1; resumed_review; unset BUREAU_DRY_RUN
record_present || fail '7 a dry run consumed the record'
echo 'PASS 7 a dry run leaves the record alone'

# Negative control: the stage without the reuse block (v3.0.0) pays again for the same inputs.
python3 - "$SCRIPTS_DIR/code-review-pipeline.sh" <<'EOF'
import sys
path = sys.argv[1]
src = open(path).read()
# v3.0.0 had no reuse: the matching record never turned into an approval.
anchor = '      REUSED_APPROVAL=1\n'
if src.count(anchor) != 1:
    sys.exit('CONTROL PATCH FAILED: reuse anchor not found')
open(path, 'w').write(src.replace(anchor, '      REUSED_APPROVAL=0\n'))
EOF
stop_review
resumed_review
[ "$(model_calls)" -gt 0 ] || fail 'negative control: without the reuse block the review still skipped the model'
echo 'PASS negative control: without reuse the same inputs pay for a full review'

echo 'OK test_review_reuse_approval'
