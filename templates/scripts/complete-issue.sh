#!/bin/bash
# Mark a Linear issue as Build Review
set -euo pipefail

source "$(dirname "$0")/bureau-config.sh"
bureau_load_env "$BUREAU_ENV_FILE" 2>/dev/null || true

bureau_secret_copy API_KEY LINEAR_API_KEY

IDENTIFIER="${1:?Usage: complete-issue.sh ${BUREAU_TEAM_KEY}-73}"

TEAM=$(echo "$IDENTIFIER" | sed 's/-[0-9]*//')
NUMBER=$(echo "$IDENTIFIER" | sed 's/[A-Z]*-//')

# Through linear_query: an unusable answer is retried and then ends this script
# with $BUREAU_EXIT_LINEAR_UNUSABLE, instead of reading as "not found".
FOUND=$(linear_query "{ issues(filter: { team: { key: { eq: \\\"$TEAM\\\" } }, number: { eq: $NUMBER } }) { nodes { id } } }")
ISSUE_ID=$(printf '%s' "$FOUND" | jq -r '.data.issues.nodes[0].id')

if [ -z "$ISSUE_ID" ] || [ "$ISSUE_ID" = "null" ]; then
  echo "$IDENTIFIER not found in Linear"
  exit 1
fi

RESULT=$(linear_query "mutation { issueUpdate(id: \\\"$ISSUE_ID\\\", input: { stateId: \\\"$BUREAU_STATE_BUILD_REVIEW\\\" }) { success } }")

SUCCESS=$(echo "$RESULT" | jq -r '.data.issueUpdate.success')

if [ "$SUCCESS" = "true" ]; then
  echo "$IDENTIFIER → Build Review"
else
  echo "Failed to update $IDENTIFIER"
  echo "$RESULT"
  exit 1
fi
