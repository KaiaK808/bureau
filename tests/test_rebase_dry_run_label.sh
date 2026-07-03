#!/bin/bash
# Regression test for the `${DRY_RUN:+ (dry-run)}` bug: when DRY_RUN is
# initialized to a non-empty string like "false", `${VAR:+...}` expands
# regardless of value, so the header would always print "(dry-run)" even on
# real runs. Asserts the header reflects the actual mode.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-200" "test-branch"
# rebase-pipeline early-exits with code 2 if states.merge isn't configured;
# enable it so we reach the header.
export BUREAU_STUB_STATE_MERGE=state-merge

# 1) Real run (no flag): header must NOT contain "(dry-run)".
run_pipeline rebase-pipeline.sh
assert_match 'Rebase Pipeline: EXP-200$'            "$LAST_STDOUT" "real run header (no dry-run label)"
if grep -qE 'Rebase Pipeline:.*\(dry-run\)' <<< "$LAST_STDOUT"; then
  echo "FAIL: real run header includes '(dry-run)' label" >&2
  grep -E 'Rebase Pipeline' <<< "$LAST_STDOUT" >&2
  exit 1
fi

# 2) Dry-run flag: header MUST contain "(dry-run)".
run_pipeline rebase-pipeline.sh --dry-run
assert_match 'Rebase Pipeline: EXP-200 \(dry-run\)' "$LAST_STDOUT" "dry-run header carries the label"

echo "OK test_rebase_dry_run_label"
