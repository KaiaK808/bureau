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
{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"Checks passed"}
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
  assert_calls_exclude 'move_issue'
  if grep -q $'pr\tmerge' "$SANDBOX/gh_calls.log"; then echo 'Unexpected merge'; exit 1; fi
  run_pipeline merge-pipeline.sh EXP-601
  assert_eq 20 "$LAST_RC" 'direct merge honors stop'
done
done
echo 'OK test_stop_before_merge'
