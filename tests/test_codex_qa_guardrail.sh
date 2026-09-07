#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
printf '{"agents":{"runner":"codex"}}' > "$TEMP_DIR/config.json"
for stage in implement qa spec_review; do
  python3 "$ROOT/templates/scripts/bureau-provider.py" --config "$TEMP_DIR/config.json" --stage "$stage" --describe | jq -e '.runner == "codex" and .sandbox == "workspace-write"' >/dev/null
done
python3 "$ROOT/templates/scripts/bureau-provider.py" --config "$TEMP_DIR/config.json" --stage code_review --describe | jq -e '.sandbox == "read-only"' >/dev/null
if BUREAU_SANDBOX_IMPLEMENT=danger-full-access python3 "$ROOT/templates/scripts/bureau-provider.py" --config "$TEMP_DIR/config.json" --stage implement --describe >/dev/null 2>&1; then
  echo 'Unexpected sandbox bypass'; exit 1
fi
echo 'OK test_codex_qa_guardrail'
