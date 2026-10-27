#!/bin/bash
# Trusted, metadata-only classification. Never pass model result/prose here.
# This vocabulary is independent of exit_class and cannot change routing.
bureau_insight_cause() {
  local code="${1:-}" outcome="${2:-}" timeout_kind="${3:-}" signal="${4:-}" withdrawn="${5:-}"
  case "$code" in
    2|26|28|29|130) printf '%s\n' other; return 0 ;;
  esac
  if [ "$withdrawn" = 1 ]; then
    case "$code" in 25) printf '%s\n' human_hold ;; *) printf '%s\n' other ;; esac
  elif [ "$code" = 0 ] && [ -z "$signal" ]; then printf '%s\n' other
  elif [ "$code" = 124 ] || { [ "$code" = 25 ] && [ "$timeout_kind" = silence ]; }; then
    printf '%s\n' timeout
  elif [ "$code" = 21 ] || [ "$code" = 11 ]; then printf '%s\n' ownership
  elif [ "$code" = 23 ] || [ "$outcome" = quota ]; then printf '%s\n' quota
  elif [ "$code" = 16 ] || [ "$code" = 22 ] || [ "$outcome" = provider-error ] || [ "$outcome" = invalid-result ]; then
    printf '%s\n' provider_error
  elif [ "$signal" = review_capped ]; then printf '%s\n' review_capped
  elif [ "$code" = 14 ] || [ "$signal" = gate_red ]; then printf '%s\n' gate_red
  elif [ "$code" = 20 ] || [ "$code" = 25 ] || [ "$signal" = human_hold ]; then printf '%s\n' human_hold
  else printf '%s\n' other
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then bureau_insight_cause "$@"; fi
