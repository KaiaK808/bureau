#!/bin/bash
# Fetch next eligible issue from Linear in Triage state
set -euo pipefail

source "$(dirname "$0")/bureau-config.sh"
source .env 2>/dev/null || true

API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

RESPONSE=$(curl -s -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: $API_KEY" \
  -d "{\"query\": \"{ issues(first: 10, filter: { labels: { id: { eq: \\\"$BUREAU_LABEL_LANE2\\\" } }, state: { id: { eq: \\\"$BUREAU_STATE_TRIAGE\\\" } } }, orderBy: createdAt) { nodes { id identifier title description priority } } }\"}")

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
curl -s -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: $API_KEY" \
  -d "{\"query\": \"mutation { issueUpdate(id: \\\"$ISSUE_ID\\\", input: { stateId: \\\"$BUREAU_STATE_BUILD\\\" }) { success } }\"}" > /dev/null

echo ""
echo "→ Moved $IDENTIFIER to Build"
