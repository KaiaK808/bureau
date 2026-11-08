#!/bin/bash
# Optional local design evidence; an unavailable installation never blocks a stage.
_BUREAU_IMPECCABLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bureau_impeccable_ui() {
  [ "$(bureau_get '.design.impeccable.enabled == true')" = true ] || return 1
  local label
  label=$(bureau_get '.linear.labels.needs_ux.name // "needs-ux"')
  { [ -n "$1" ] && [ -f "${1%/}/design.md" ]; } || printf '%s' "$ISSUE_DETAIL" \
    | jq -e --arg label "$label" '.labels | index($label) != null or index("needs-ux") != null' >/dev/null
}

bureau_impeccable() {
  ( bureau_without_secrets python3 -I "$_BUREAU_IMPECCABLE_DIR/bureau-impeccable.py" "$1" \
      --repo "$PWD" --config "${BUREAU_CONFIG:-$SCRIPT_REPO/.bureau.json}" "${@:2}" )
}

bureau_impeccable_policy() {
  echo 'Bureau owns Impeccable engine execution. Do not invoke Impeccable commands, hooks or agents yourself; use local reference reading only, without its network or browser workflows.'
}

bureau_impeccable_check() {
  local tier="$1" report
  IMPECCABLE_GATE_BLOCKED=0
  IMPECCABLE_FEEDBACK=""
  report=$(bureau_impeccable detect --tier "$tier" --cache "$IMPLEMENT_GATE_TMP/impeccable-all.json") \
    || report='{"status":"not measured","reason":"Bureau detector helper could not run"}'
  IMPECCABLE_FEEDBACK="Impeccable machine evidence ($tier): $report"
  case "$(printf '%s' "$report" | jq -r '.status')" in
    clean|'not applicable') ;;
    findings)
      IMPECCABLE_GATE_BLOCKED=1
      IMPECCABLE_FEEDBACK+=$'\nFix these findings or record an intentional exception in .impeccable/bureau-ignore.json: an array of objects with exactly file (exact repository-relative UI file), antipattern (one rule) and reason (named, nonempty justification). Broad and reasonless ignores are rejected.' ;;
    'not measured')
      # Missing/mismatched optional tooling is informational; a verified
      # detector's failed measurement cannot satisfy the design gate.
      [ "$(printf '%s' "$report" | jq -r '.availability // empty')" = unavailable ] || IMPECCABLE_GATE_BLOCKED=1 ;;
    *) IMPECCABLE_GATE_BLOCKED=1 ;;
  esac
  echo "$IMPECCABLE_FEEDBACK"
}
