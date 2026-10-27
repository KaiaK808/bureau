#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP
sandbox_init EXP-601 test-branch
trap teardown EXIT
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
export BUREAU_STUB_ISSUE_STATE='Build Review'
export GH_STUB_EXISTING_PR=99
for stop_flag in BUREAU_NO_MERGE BUREAU_STOP_REQUESTED; do
export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
export "$stop_flag=1"
# A legacy environment is sourced after configuration and runtime re-entry.
# Clearing both public settings must not revoke an explicit caller boundary.
cat > "$SANDBOX/.env" <<'EOF'
LINEAR_API_KEY=test-key
BUREAU_NO_MERGE=0
BUREAU_STOP_REQUESTED=0
EOF
for merge_mode in disabled enabled; do
  if [ "$merge_mode" = enabled ]; then
    export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
  else
    export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  fi
  python3 "$SCRIPTS_DIR/bureau-supervision.py" --repo "$SANDBOX" resume EXP-601 >/dev/null
  run_pipeline code-review-pipeline.sh EXP-601
  if [ "$LAST_RC" != 20 ]; then printf '%s\n%s\n' "$LAST_STDOUT" "$LAST_STDERR"; fi
  assert_eq 20 "$LAST_RC" "review stops before merge ($stop_flag, $merge_mode)"
  assert_file_contains '<!-- bureau-acceptance: pr=99 head=' "$SANDBOX/briefs-ticket.log" 'requested stop ticket brief missing'
  assert_file_contains '<!-- bureau-acceptance: pr=99 head=' "$SANDBOX/briefs-pr.log" 'requested stop PR brief missing'
  assert_eq 1 "$(grep -c '<!-- bureau-acceptance:' "$SANDBOX/briefs-pr.log")" 'same requested stop PR/head delivered once'
  assert_calls_exclude 'move_issue'
  if grep -q $'pr\tmerge' "$SANDBOX/gh_calls.log"; then echo 'Unexpected merge'; exit 1; fi
  run_pipeline merge-pipeline.sh EXP-601
  assert_eq 20 "$LAST_RC" 'direct merge honors stop'
done
done
teardown

# Keep the saved boundary between all retries. Each stop flag and missing
# destination must retry without another review, state change or merge.
for stop_flag in BUREAU_NO_MERGE BUREAU_STOP_REQUESTED; do
for destination in ticket pr; do
  sandbox_init EXP-601 test-branch
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  export "$stop_flag=1"
  export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  export BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -qm 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  printf '%s\n' '{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"missing_acceptance":[],"fixes_needed":[],"comment":"Review checked.","summary":"Checks passed"}' > "$SANDBOX/approve.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt" FAKE_CLAUDE_LOG="$SANDBOX/model-calls.log"
  if [ "$destination" = ticket ]; then export BUREAU_STUB_BRIEF_TICKET_FAIL=1
  else export GH_STUB_BRIEF_FAIL=1; fi
  run_pipeline code-review-pipeline.sh EXP-601
  assert_eq 20 "$LAST_RC" 'initial publication failure preserves requested stop'
  cp "$SANDBOX/.git/bureau/review-stops.json" "$SANDBOX/saved-boundary.json"
  cp "$SANDBOX/model-calls.log" "$SANDBOX/saved-model-calls.log"
  for retry in pending recovered deduplicated; do
    if [ "$retry" != pending ]; then unset BUREAU_STUB_BRIEF_TICKET_FAIL GH_STUB_BRIEF_FAIL; fi
    : > "$SANDBOX/gh_calls.log"
    run_pipeline code-review-pipeline.sh EXP-601
    assert_eq 20 "$LAST_RC" "saved boundary preserves exit ($stop_flag/$destination/$retry)"
    cmp "$SANDBOX/saved-boundary.json" "$SANDBOX/.git/bureau/review-stops.json"
    cmp "$SANDBOX/saved-model-calls.log" "$SANDBOX/model-calls.log"
    assert_calls_exclude 'move_issue'
    if grep -q $'pr\tmerge' "$SANDBOX/gh_calls.log"; then echo 'Unexpected merge'; exit 1; fi
    # A delivered destination is skipped even for the marker lookup.
    if [ "$destination" = ticket ] || [ "$retry" = deduplicated ]; then
      if grep -q -- $'--json\tcomments' "$SANDBOX/gh_calls.log"; then echo 'Unexpected delivered PR retry'; exit 1; fi
    else
      assert_file_contains $'pr\tview\t99\t--json\tcomments' "$SANDBOX/gh_calls.log" 'pending PR lookup missing'
    fi
    if [ "$destination" = pr ] || [ "$retry" = deduplicated ]; then
      assert_calls_exclude 'get_issue_comments'
    else
      assert_calls_include 'get_issue_comments'
    fi
    if [ "$retry" != pending ]; then
      for file in briefs-ticket.log briefs-pr.log; do
        assert_eq 1 "$(grep -c '<!-- bureau-acceptance:' "$SANDBOX/$file")" 'only missing destination delivered once'
      done
    fi
  done
  unset FAKE_CLAUDE_LOG
  teardown
done
done
trap - EXIT  # Every fixture above has already been torn down.
echo 'OK test_stop_before_merge'
