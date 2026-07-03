#!/bin/bash
# Same regression check for merge-pipeline.sh — the same `${DRY_RUN:+...}`
# pattern was duplicated there. If a future edit reintroduces the bug, this
# test catches it.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

sandbox_init "EXP-201" "test-branch"
export BUREAU_STUB_STATE_MERGE=state-merge

run_pipeline merge-pipeline.sh
assert_match 'Merge Pipeline: EXP-201$'            "$LAST_STDOUT" "real run header (no dry-run label)"
if grep -qE 'Merge Pipeline:.*\(dry-run\)' <<< "$LAST_STDOUT"; then
  echo "FAIL: real run header includes '(dry-run)' label" >&2
  grep -E 'Merge Pipeline' <<< "$LAST_STDOUT" >&2
  exit 1
fi

run_pipeline merge-pipeline.sh --dry-run
assert_match 'Merge Pipeline: EXP-201 \(dry-run\)' "$LAST_STDOUT" "dry-run header carries the label"

echo "OK test_merge_dry_run_label"
