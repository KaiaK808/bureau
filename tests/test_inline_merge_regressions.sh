#!/bin/bash
# The regression cases of the review stage's inline merge: cleanup retries, fork heads,
# delayed state reads, handoff confirmation, merge result reconciliation, verdict head
# binding and worktree release. They share the fixtures of test_inline_merge_result.sh.
set -euo pipefail
exec bash "$(dirname "$0")/test_inline_merge_result.sh" regressions
