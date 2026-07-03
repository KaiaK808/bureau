#!/bin/bash
# RESEARCH path: when `needs-research` is on the issue AND .agents.research is
# enabled, spec-pipeline runs a pre-spec research call, posts the digest as a
# Linear comment (carrying the bureau-research marker), and strips the label.
set -uo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-100" "test-branch"

export BUREAU_STUB_ISSUE_STATE="Triage"
export BUREAU_STUB_LABELS='["needs-research","lane-2"]'
export BUREAU_STUB_AGENT_ENABLED="research"
# First $CLAUDE call is research → marker fixture. Remaining calls (specify,
# plan, tasks, digest) just need non-empty output; fake_claude.sh repeats the
# last fixture beyond the listed count.
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_research.txt:$FIXTURES_DIR/claude_filler.txt"

run_pipeline spec-pipeline.sh "EXP-100"

assert_match 'Phase 0/5: research' "$LAST_STDOUT" "research phase header printed"
assert_match 'research complete; label stripped' "$LAST_STDOUT" "research success line printed"

assert_calls_include 'post_comment.*bureau-research' "research digest comment posted"
assert_calls_include 'remove_issue_label.*needs-research' "needs-research label stripped"

echo "OK test_spec_research_fires"
