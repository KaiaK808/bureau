#!/bin/bash
# Phantom-prevention: when add_issue_label fails (Linear API hiccup), the
# pipeline must NOT write to escalations.log. log_escalation is only invoked
# on the success branch of the `if add_issue_label … then` chain.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-104" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_no_progress.txt"
export BUREAU_IMPL_MAX_ITER=3
# Force the stub's add_issue_label to return non-zero, simulating a Linear
# API failure mid-escalation.
export BUREAU_STUB_ADD_LABEL_RC=1

run_implement_pipeline

assert_eq 0 "$LAST_RC" "exit code (failure to label is non-fatal, retries next tick)"

# add_issue_label was attempted ...
assert_calls_include 'add_issue_label.*needs-human' "label attempt recorded"
# ... but log_escalation was NOT called.
assert_calls_exclude '^log_escalation' "no phantom escalation logged"

# And the TSV file should be empty / missing.
if [ -s "$SANDBOX/logs/escalations.log" ]; then
  echo "FAIL: escalations.log is non-empty despite Linear failure" >&2
  cat "$SANDBOX/logs/escalations.log" >&2
  exit 1
fi

# Operator should still see the WARN on stderr so the failure isn't silent.
assert_match "WARN: failed to add 'needs-human' label" "$LAST_STDERR" "WARN surfaced to stderr"

echo "OK test_escalation_log_silent_on_linear_failure"
