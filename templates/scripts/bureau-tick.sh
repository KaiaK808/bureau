#!/bin/bash
# At most one eligible stage per invocation. Suitable for native app scheduling.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
source scripts/bureau-config.sh
[ ! -f "$BUREAU_ENV_FILE" ] || bureau_load_env --export "$BUREAU_ENV_FILE"
MODE=all
RESULT_FILE="${BUREAU_TICK_RESULT:-$REPO_DIR/logs/bureau-tick.json}"
export BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --stage) MODE="$2"; shift 2 ;;
    --result-file) RESULT_FILE="$2"; shift 2 ;;
    --no-merge) export BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=1; shift ;;
    --allow-merge) export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0; shift ;;
    *) echo "Unknown tick argument $1" >&2; exit 1 ;;
  esac
done
ISSUE="" STAGE="" BEFORE="" AFTER="" RC=0 OUTCOME=waiting
SKIPPED_REVIEWS='[]'
write_result() {
  local tmp
  mkdir -p "$(dirname "$RESULT_FILE")"
  tmp=$(mktemp "${RESULT_FILE}.XXXXXX")
  jq -n --arg outcome "$OUTCOME" --arg issue "$ISSUE" --arg stage "$STAGE" \
    --arg before "$BEFORE" --arg after "$AFTER" --argjson exit_code "$RC" --argjson skipped_reviews "$SKIPPED_REVIEWS" \
    '{version:1,outcome:$outcome,issue:$issue,stage:$stage,before:$before,after:$after,exit_code:$exit_code,skipped_reviews:$skipped_reviews}' > "$tmp"
  mv "$tmp" "$RESULT_FILE"
  cat "$RESULT_FILE"
}
if bureau_is_paused; then OUTCOME=paused; write_result; exit 0; fi
case "$MODE" in all|spec|spec_review|ux|copy|implement|qa|code_review|merge|rebase) ;; *) echo 'Unknown stage' >&2; exit 1 ;; esac
if ! (precondition_linear); then RC=10; OUTCOME=failed; write_result; exit 10; fi
# A ticket waiting on its merge gate (merge_gate_waits in bureau-config.sh) does not take
# the tick while another stage has work (v3.2). With --allow-merge over all stages, a stage
# whose only pick is such a ticket is passed over (pipeline_pick_next marks that pick), and
# no later stage of that pass picks the ticket either: the rebase stage shares the merge
# picker and is never held, and on a gate that is only not yet decided it would end 2 on the
# same ticket. The stages passed over pick again, in their order and as before, once no other
# stage had a ticket to run. With --stage there is no other stage; --no-merge runs no merge
# stage and skips a review held at its boundary (below): both pick as before.
PASS_HELD="" PASSED="" PASSED_IDS=""
if [ "$MODE" = all ] && [ "$BUREAU_NO_MERGE" = 0 ]; then PASS_HELD="mark-held"; fi
# tick_stage <stage> [mark-held]: run one ticket of <stage>, write the result and exit;
# return when the stage has no ticket to run now.
tick_stage() {
  local stage="$1" mark="${2:-}" pipeline
  [ "$MODE" = all ] || [ "$MODE" = "$stage" ] || return 0
  agent_enabled "$stage" || return 0
  if [ "$stage" = merge ] && [ "$BUREAU_NO_MERGE" = 1 ]; then return 0; fi
  pipeline="$(printf '%s' "$stage" | tr '_' '-')-pipeline.sh"
  SKIPPED="${mark:+$PASSED_IDS}"
  while :; do
    if ! ISSUE=$(pipeline_pick_next "$pipeline" "$SKIPPED" ${mark:+"$mark"}); then RC=10; OUTCOME=failed; write_result; exit "$RC"; fi
    case "$ISSUE" in *" held")
      ISSUE=${ISSUE% held}
      echo "tick: $ISSUE waits on its merge gate and is the only $stage ticket that can be picked — passed over while another stage has work" >&2
      PASSED="${PASSED:+$PASSED }$stage"
      PASSED_IDS="${PASSED_IDS:+$PASSED_IDS,}$ISSUE"
      ISSUE=""
      return 0 ;;
    esac
    [ -n "$ISSUE" ] || break
    # A bounded tick may skip several unchanged review boundaries but execute
    # only one stage. The picker must walk past held tickets in this same queue.
    case ",$SKIPPED," in *",$ISSUE,"*) RC=10; OUTCOME=failed; write_result; exit "$RC" ;; esac
    STAGE="$stage"
    if ! BEFORE=$(get_issue_state "$ISSUE") || ! BRANCH=$(get_issue_branch "$ISSUE"); then
      RC=10; OUTCOME=failed; write_result; exit "$RC"
    fi
    if [ "$stage" = code_review ] && [ "$BUREAU_NO_MERGE" = 1 ]; then
      if ! DETAIL=$(get_issue_detail "$ISSUE"); then
        RC=10; OUTCOME=failed; write_result; exit "$RC"
      fi
      if ! STOP=$(printf '%s' "$DETAIL" | python3 -I scripts/bureau-supervision.py check "$ISSUE" --branch "$BRANCH" --state "$BEFORE"); then
        RC=18; OUTCOME=failed; write_result; exit "$RC"
      fi
      if [ "$(printf '%s' "$STOP" | jq -r .stopped)" = true ]; then
        SKIPPED="${SKIPPED:+$SKIPPED,}$ISSUE"
        SKIPPED_REVIEWS=$(printf '%s' "$SKIPPED_REVIEWS" | jq --arg issue "$ISSUE" '. + [$issue]')
        ISSUE="" STAGE="" BEFORE=""
        continue
      fi
    fi
    break
  done
  [ -n "$ISSUE" ] || return 0
  case "$stage" in merge|rebase) ;; *)
    if ! BUREAU_THROTTLE_ONCE=1 session_throttle_guard "$stage"; then
      OUTCOME=waiting; RC=23; write_result; exit 0
    fi ;;
  esac
  if ! WORKTREE=$(python3 -I scripts/bureau-supervision.py workspace "$ISSUE" --stage "$stage" | jq -r .workspace); then
    RC=21; OUTCOME=blocked; write_result; exit "$RC"
  fi
  if bash scripts/bureau-worker.sh "$ISSUE" "$pipeline" "$WORKTREE" "$BRANCH" >&2; then RC=0; else RC=$?; fi
  AFTER=$(get_issue_state "$ISSUE" || true)
  case "$RC" in
    0)
      # Several legacy stages return zero after parking a ticket. Verify the
      # actual post-stage state/labels before reporting an uneventful wait.
      if [ -z "$AFTER" ] || ! DETAIL=$(get_issue_detail "$ISSUE") || ! printf '%s' "$DETAIL" | jq -e --arg issue "$ISSUE" '.identifier == $issue and (.labels | type == "array") and all(.labels[]; type == "string")' >/dev/null; then
        RC=10; OUTCOME=failed
      elif [ "$AFTER" = Done ]; then OUTCOME=completed
      elif [ "$AFTER" = Cancelled ] || [ "$AFTER" = Canceled ] || [ "$AFTER" = Duplicate ]; then RC=26; OUTCOME=blocked
      else
        HUMAN_LABEL=$(bureau_get '.linear.labels.needs_human.name // "needs-human"')
        if printf '%s' "$DETAIL" | jq -e --arg human "$HUMAN_LABEL" '.labels | any(. == $human or . == "needs-human" or . == "blocked" or . == "wip")' >/dev/null; then
          RC=25; OUTCOME=blocked
        elif [ "$AFTER" != "$BEFORE" ]; then OUTCOME=advanced
        else OUTCOME=waiting; fi
      fi ;;
    2) OUTCOME=waiting ;;
    20) OUTCOME=stopped_for_review ;;
    21|25|26) OUTCOME=blocked ;;
    *) OUTCOME=failed ;;
  esac
  write_result
  exit "$RC"
}
for stage in merge rebase code_review qa implement copy ux spec_review spec; do
  tick_stage "$stage" "$PASS_HELD"
done
for stage in $PASSED; do
  echo "tick: no other stage has work — back to $stage" >&2
  tick_stage "$stage"
done
write_result
