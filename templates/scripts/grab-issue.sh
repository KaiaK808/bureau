#!/bin/bash
# Fetch next eligible issue from Linear in Triage state
set -euo pipefail

source "$(dirname "$0")/bureau-config.sh"
bureau_load_env "$BUREAU_ENV_FILE" 2>/dev/null || true

API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

# Through linear_query: an unusable answer is retried and then ends this script
# with $BUREAU_EXIT_LINEAR_UNUSABLE, instead of reading as "nothing in Triage".
RESPONSE=$(linear_query "{ issues(first: 10, filter: { labels: { id: { eq: \\\"$BUREAU_LABEL_LANE2\\\" } }, state: { id: { eq: \\\"$BUREAU_STATE_TRIAGE\\\" } } }, orderBy: createdAt) { nodes { id identifier title description priority } } }")

BEST=$(echo "$RESPONSE" | jq -r '[.data.issues.nodes[] | select(.id) | .priority = (if .priority == 0 then 99 else .priority end)] | sort_by(.priority) | first')

ISSUE_ID=$(echo "$BEST" | jq -r '.id // empty')
IDENTIFIER=$(echo "$BEST" | jq -r '.identifier // empty')
TITLE=$(echo "$BEST" | jq -r '.title // empty')
DESC=$(echo "$BEST" | jq -r '.description // empty')

if [ -z "$ISSUE_ID" ]; then
  echo "No $BUREAU_LABEL_LANE2_NAME issues in Triage. Nothing to do."
  exit 0
fi

echo "=== $IDENTIFIER: $TITLE ==="
echo ""
echo "$DESC"

# Move to Build state
MOVED=$(linear_query "mutation { issueUpdate(id: \\\"$ISSUE_ID\\\", input: { stateId: \\\"$BUREAU_STATE_BUILD\\\" }) { success } }")
if [ "$(printf '%s' "$MOVED" | jq -r '.data.issueUpdate.success // false')" != "true" ]; then
  echo "Failed to move $IDENTIFIER to Build" >&2
  exit 1
fi

echo ""
echo "→ Moved $IDENTIFIER to Build"
