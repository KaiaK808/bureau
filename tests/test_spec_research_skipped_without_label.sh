#!/bin/bash
# SKIP path: without the `needs-research` label, spec-pipeline runs no research
# call even when .agents.research is enabled in .bureau.json. No marker comment
# posted, no label-strip attempted.
set -uo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-101" "test-branch"

export BUREAU_STUB_ISSUE_STATE="Triage"
export BUREAU_STUB_LABELS='["lane-2"]'           # no needs-research
export BUREAU_STUB_AGENT_ENABLED="research"      # enabled, but label absent
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_filler.txt"

run_pipeline spec-pipeline.sh "EXP-101"

if grep -qE 'Phase 0/5: research' <<< "$LAST_STDOUT"; then
  echo "FAIL: research phase ran without needs-research label" >&2
  printf '%s\n' "$LAST_STDOUT" | sed 's/^/  | /' >&2
  exit 1
fi

assert_match 'Phase 1/5: specify' "$LAST_STDOUT" "specify phase reached"
assert_calls_exclude 'post_comment.*bureau-research' "no research digest posted"
assert_calls_exclude 'remove_issue_label.*needs-research' "no label strip attempted"

echo "OK test_spec_research_skipped_without_label"
