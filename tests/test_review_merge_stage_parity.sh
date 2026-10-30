#!/bin/bash
# Both merge routes share approval persistence, stage exits and hand-offs.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP
ISSUE=EXP-1760

for mode in code fallback; do
  for boundary in stop pending blocked manual worker; do
    sandbox_init "$ISSUE" test-branch
    printf 'change\n' > "$SANDBOX/change.txt"
    git -C "$SANDBOX" add change.txt
    git -C "$SANDBOX" commit -qm 'fixture change'
    git -C "$SANDBOX" push -q origin test-branch
    printf '%s\n' '{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"missing_acceptance":[],"fixes_needed":[],"comment":"Review checked.","summary":"Fixture review."}' > "$SANDBOX/merger.json"
    export FAKE_CLAUDE_FIXTURES="$SANDBOX/merger.json" FAKE_CLAUDE_MERGE_FIXTURE="$SANDBOX/merger.json"
    export FAKE_CLAUDE_LOG="$SANDBOX/model-calls.log"
    export BUREAU_STUB_ISSUE_STATE='Build Review' BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
    export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
    if [ "$mode" = code ]; then review_clean_specialists; else review_fallback_specialists; fi
    pr2_gate_setup
    pr2_config '.repo.test_command = "true"'
    expected=0
    case "$boundary" in
      stop) export BUREAU_NO_MERGE=1; expected=20 ;;
      pending) pr2_checks none; expected=2 ;;
      blocked) pr2_checks red; expected=25 ;;
      manual) export BUREAU_STUB_STATE_MERGE=state-merge; pr2_config '.agents.merge_mode = "manual"' ;;
      worker) export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge ;;
    esac
    run_pipeline code-review-pipeline.sh "$ISSUE"
    assert_eq "$expected" "$LAST_RC" "$mode/$boundary exit"
    expected_calls=3; [ "$mode" != fallback ] || expected_calls=4
    assert_eq "$expected_calls" "$(wc -l < "$FAKE_CLAUDE_LOG" | tr -d ' ')" "$mode/$boundary provider calls"
    pr2_merged && { echo "FAIL: $mode/$boundary merged across its boundary" >&2; exit 1; }
    case "$boundary" in
      stop|pending|blocked)
        stops="$SANDBOX/.git/bureau/review-stops.json"
        assert_eq APPROVE "$(jq -r --arg i "$ISSUE" '.[$i].verdict' "$stops")" "$mode/$boundary saved approval"
        wait=false; [ "$boundary" != pending ] || wait=true
        assert_eq "$wait" "$(jq -r --arg i "$ISSUE" '.[$i].merge_gate_wait // false' "$stops")" "$mode/$boundary saved gate wait"
        assert_calls_exclude '^move_issue'
        if [ "$boundary" = blocked ]; then
          assert_calls_include $'add_issue_label\t'"$ISSUE"$'\tneeds-human'
        else
          assert_calls_exclude $'add_issue_label\t'"$ISSUE"$'\tneeds-human'
        fi
        cp "$FAKE_CLAUDE_LOG" "$SANDBOX/first-calls.log"
        run_pipeline code-review-pipeline.sh "$ISSUE"
        assert_eq "$expected" "$LAST_RC" "$mode/$boundary retry exit"
        cmp "$SANDBOX/first-calls.log" "$FAKE_CLAUDE_LOG"
        ;;
      manual|worker)
        assert_calls_include $'move_issue\t'"$ISSUE"$'\tstate-merge'
        assert_calls_exclude $'move_issue\t'"$ISSUE"$'\tstate-done'
        ;;
    esac
    echo "PASS $mode/$boundary: exit $expected, approval and hand-off preserved"
    teardown
  done
done
trap - EXIT  # Every sandbox above has already been removed.
echo 'OK test_review_merge_stage_parity'
