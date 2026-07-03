#!/bin/bash
# Mark a Linear issue as Build Review
set -euo pipefail

source "$(dirname "$0")/bureau-config.sh"
source .env 2>/dev/null || true

API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

IDENTIFIER="${1:?Usage: complete-issue.sh ${BUREAU_TEAM_KEY}-73}"

TEAM=$(echo "$IDENTIFIER" | sed 's/-[0-9]*//')
NUMBER=$(echo "$IDENTIFIER" | sed 's/[A-Z]*-//')

ISSUE_ID=$(curl -s -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: $API_KEY" \
  -d "{\"query\": \"{ issues(filter: { team: { key: { eq: \\\"$TEAM\\\" } }, number: { eq: $NUMBER } }) { nodes { id } } }\"}" \
  | jq -r '.data.issues.nodes[0].id')

if [ -z "$ISSUE_ID" ] || [ "$ISSUE_ID" = "null" ]; then
  echo "$IDENTIFIER not found in Linear"
  exit 1
fi

RESULT=$(curl -s -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: $API_KEY" \
  -d "{\"query\": \"mutation { issueUpdate(id: \\\"$ISSUE_ID\\\", input: { stateId: \\\"$BUREAU_STATE_BUILD_REVIEW\\\" }) { success } }\"}")

SUCCESS=$(echo "$RESULT" | jq -r '.data.issueUpdate.success')

if [ "$SUCCESS" = "true" ]; then
  echo "$IDENTIFIER → Build Review"
else
  echo "Failed to update $IDENTIFIER"
  echo "$RESULT"
  exit 1
fi
