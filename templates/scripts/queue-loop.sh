#!/bin/bash
# Queue worker — spawns fresh Claude Code sessions to process Linear issues.
# Reads agent config from .bureau.json.
#
# Usage:
#   ./scripts/queue-loop.sh              # runs enabled agents (default)
#   ./scripts/queue-loop.sh spec         # only spec stage
#   ./scripts/queue-loop.sh all 15       # all enabled agents, 15 min interval

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

# Load .env so LINEAR_API_KEY, TELEGRAM_* etc are available to pipelines
if [ -f "$REPO_DIR/.env" ]; then
  set -a; source "$REPO_DIR/.env"; set +a
fi
API_KEY="${LINEAR_API_KEY:-}"

# --dry-run flag (env-var BUREAU_DRY_RUN=1 also honoured) flips bureau-config.sh's
# move_issue / post_comment / add_issue_label / alert_telegram into log-only mode
# AND each pipeline's git-push / gh-pr-create steps. Filter it out before reading
# positional args so MODE/INTERVAL stay correct regardless of flag position.
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) export BUREAU_DRY_RUN=1 ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done
if [ "${#POSITIONAL[@]}" -gt 0 ]; then set -- "${POSITIONAL[@]}"; else set --; fi

MODE="${1:-all}"
INTERVAL_MINUTES="${2:-$BUREAU_POLL_INTERVAL}"
INTERVAL_SECONDS=$((INTERVAL_MINUTES * 60))
LOG_DIR="$REPO_DIR/logs"
LOG_FILE="$LOG_DIR/queue-$MODE.log"
WORKTREE_DIR="$REPO_DIR/.worktrees/queue-$MODE"

mkdir -p "$LOG_DIR"

echo "=== Queue Worker Started ==="
echo "Repo: $REPO_DIR"
echo "Mode: $MODE"
echo "Team: $BUREAU_TEAM_NAME ($BUREAU_TEAM_KEY)"
echo "Interval: ${INTERVAL_MINUTES}m"
echo "Log: $LOG_FILE"
[ "${BUREAU_DRY_RUN:-0}" = "1" ] && echo "DRY-RUN: no Linear/GitHub mutations will be made"
echo "Stop with Ctrl+C"
echo ""

# free_branch_from_other_worktrees() is defined in bureau-config.sh so all
# pipeline scripts can call it directly before their own checkout -B.

# reset_worktree and exit_class were relocated to bureau-config.sh so that
# single-shot drivers (shepherd.sh) can reuse them without sourcing the loop.
# Both functions remain in scope because queue-loop.sh sources bureau-config.sh
# at the top of this file.

# Pre-pick an issue (matching what the pipeline would pick) so we can
# resolve its spec branch and pre-reset the worktree to the right place.
# Returns the issue identifier on stdout, empty if no work.
#
# The (state, required-labels, exclude-labels) per pipeline lives in the
# pipeline_picker_args registry in bureau-config.sh — see that file for the
# rationale. queue-loop.sh and each pipeline both go through the same
# registry, so adding/changing an agent only touches one place.
preselect_issue() {
  pipeline_pick_next "$1"
}

run_script() {
  local script="$1"
  local label="$2"
  local wt="$3"
  local TIMESTAMP
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
  echo "[$TIMESTAMP] $label..." | tee -a "$LOG_FILE"

  # EXP-415 Part A: preselect and reset worktree to a known state.
  local picked=""
  local target_branch=""
  picked=$(preselect_issue "$script" 2>/dev/null || true)
  if [ -n "$picked" ]; then
    echo "[$TIMESTAMP] $label — candidate: $picked" | tee -a "$LOG_FILE"
    target_branch=$(get_issue_branch "$picked" 2>/dev/null || true)
  fi
  reset_worktree "$wt" "$script" "$target_branch"

  # logs→memory: emit stage_start only when there's a real candidate so idle
  # queue-empty ticks don't bloat events.jsonl. Track wall time for the
  # matching stage_end event so /bureau-learnings can surface stage timing.
  local start_epoch=0
  if [ -n "$picked" ]; then
    start_epoch=$(date +%s)
    emit_event "event=stage_start" "mode=$MODE" "stage=$script" \
      "issue=$picked" "branch=${target_branch:-}"
  fi

  local exit_code=0
  cd "$wt"
  if [ -n "$picked" ]; then
    "$REPO_DIR/scripts/$script" "$picked" 2>&1 | tee -a "$LOG_FILE"
  else
    "$REPO_DIR/scripts/$script" 2>&1 | tee -a "$LOG_FILE"
  fi
  exit_code=${PIPESTATUS[0]}
  cd "$REPO_DIR"

  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
  local klass
  klass=$(exit_class "$exit_code")
  if [ -n "$picked" ]; then
    local duration_s=$(( $(date +%s) - start_epoch ))
    emit_event "event=stage_end" "mode=$MODE" "stage=$script" \
      "issue=$picked" "branch=${target_branch:-}" \
      "exit_code=$exit_code" "class=$klass" "duration_s=$duration_s"
  fi
  case "$exit_code" in
    0)  echo "[$TIMESTAMP] $label done." | tee -a "$LOG_FILE" ;;
    2)  echo "[$TIMESTAMP] $label — queue empty." | tee -a "$LOG_FILE" ;;
    *)
        echo "[$TIMESTAMP] $label — error (exit $exit_code / $klass)." | tee -a "$LOG_FILE"
        local tail_log
        tail_log=$(tail -n 20 "$LOG_FILE" 2>/dev/null || true)
        alert_telegram "${picked:-none}" "$script" "$exit_code" \
          "$label failed ($klass)" "$tail_log" || true
        ;;
  esac
  echo "---" | tee -a "$LOG_FILE"
  return $exit_code
}

run_one() {
  local script="$1"
  local label="$2"
  local wt="$3"
  run_script "$script" "$label" "$wt"
}

while true; do
  DID_WORK=false

  case "$MODE" in
    spec)
      run_one "spec-pipeline.sh" "Spec (Triage → Spec Review)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    spec-review)
      run_one "spec-review-pipeline.sh" "Spec Review (Spec Review → Build/Design)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    ux)
      run_one "ux-pipeline.sh" "UX/UI Design (Design → Build)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    copy)
      run_one "copy-pipeline.sh" "Copy (Copy → Build)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    implement)
      run_one "implement-pipeline.sh" "Implement (Build → QA/Build Review)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    qa)
      run_one "qa-pipeline.sh" "QA (QA → Build Review)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    code-review)
      run_one "code-review-pipeline.sh" "Code Review (Build Review → Merge|Done)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    rebase)
      run_one "rebase-pipeline.sh" "Rebase (DIRTY bureau-only PRs → Build Review)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    merge)
      run_one "merge-pipeline.sh" "Merge (gated PR merger, Merge → Done)" "$WORKTREE_DIR" && DID_WORK=true
      ;;
    all)
      # EXP-491: drain before refilling. Fan-out order is REVERSED from state-
      # machine sequence — merge/rebase first, spec last. Reasoning: when
      # multiple stages have pickable issues, prefer the ones closest to Done
      # so existing tickets clear before new ones enter. Without this, an
      # active queue keeps adding spec work faster than merge can drain it,
      # which compounds branch divergence (every new branch races origin/main).
      # All opt-in pipelines (qa, copy, merge, rebase) exit 2 when their
      # respective state / label are not configured in .bureau.json.
      agent_enabled "merge" && run_one "merge-pipeline.sh" "Merge" "$REPO_DIR/.worktrees/queue-merge" && DID_WORK=true
      agent_enabled "rebase" && run_one "rebase-pipeline.sh" "Rebase" "$REPO_DIR/.worktrees/queue-rebase" && DID_WORK=true
      agent_enabled "code_review" && run_one "code-review-pipeline.sh" "Code Review" "$REPO_DIR/.worktrees/queue-code-review" && DID_WORK=true
      agent_enabled "qa" && run_one "qa-pipeline.sh" "QA" "$REPO_DIR/.worktrees/queue-qa" && DID_WORK=true
      agent_enabled "implement" && run_one "implement-pipeline.sh" "Implement" "$REPO_DIR/.worktrees/queue-implement" && DID_WORK=true
      agent_enabled "copy" && run_one "copy-pipeline.sh" "Copy" "$REPO_DIR/.worktrees/queue-copy" && DID_WORK=true
      agent_enabled "ux" && run_one "ux-pipeline.sh" "UX/UI Design" "$REPO_DIR/.worktrees/queue-ux" && DID_WORK=true
      agent_enabled "spec_review" && run_one "spec-review-pipeline.sh" "Spec Review" "$REPO_DIR/.worktrees/queue-spec-review" && DID_WORK=true
      agent_enabled "spec" && run_one "spec-pipeline.sh" "Spec (Triage → Spec Review)" "$REPO_DIR/.worktrees/queue-spec" && DID_WORK=true
      ;;
    *)
      echo "Unknown mode: $MODE"
      echo "Available: spec, spec-review, ux, copy, implement, qa, code-review, merge, rebase, all"
      exit 1
      ;;
  esac

  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
  if [ "$DID_WORK" = true ]; then
    echo "[$TIMESTAMP] Queues drained. Next check in ${INTERVAL_MINUTES}m." | tee -a "$LOG_FILE"
  else
    echo "[$TIMESTAMP] All queues empty. Next check in ${INTERVAL_MINUTES}m." | tee -a "$LOG_FILE"
  fi
  sleep "$INTERVAL_SECONDS"
done
