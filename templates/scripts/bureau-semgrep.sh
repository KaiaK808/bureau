#!/bin/bash
# Trusted shell entry point, following the sibling Impeccable helper convention.
_BUREAU_SEMGREP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bureau_semgrep() {
  local command="$1" open_prs=null result receipt_path rc=0
  shift
  if [ "$command" = scan ]; then
    # Failed inventory keeps all PR-referenced attempts; it never deletes evidence
    # on the assumption that an unreachable PR is closed.
    open_prs=$(gh api --paginate 'repos/{owner}/{repo}/pulls?state=open&per_page=100' 2>/dev/null \
      | jq -sce 'if length > 0 and all(.[]; type == "array" and all(.[]; (.number | type) == "number")) then [.[][] | .number] else error("invalid inventory") end') \
      || open_prs=null
  fi
  result=$(bureau_without_secrets python3 -I "$_BUREAU_SEMGREP_DIR/bureau-semgrep.py" "$command" \
      --repo "$PWD" --config "${BUREAU_CONFIG:-$SCRIPT_REPO/.bureau.json}" --open-prs "$open_prs" "$@") || rc=$?
  [ "$rc" = 0 ] || return "$rc"
  if [ "$command" = scan ] && [ -n "${BUREAU_RUN_ID:-}" ] && [ -n "${BUREAU_STAGE_ATTEMPT_ID:-}" ]; then
    receipt_path=$(printf '%s' "$result" | jq -er '.receipt_path') || return 24
    bureau_evidence evidence-gate --kind artifact --path "$receipt_path" --head '' --end-head '' \
      --command 'Semgrep scanner receipt' --command-id review.artifact || true
  fi
  printf '%s\n' "$result"
}
