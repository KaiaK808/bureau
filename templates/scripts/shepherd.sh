#!/bin/bash
# shepherd.sh — single-ticket end-to-end driver.
#
# Drives one Linear ticket sequentially through every pipeline phase, in the
# foreground, with all agents forced on so the run is "really end to end."
# Opposite mental model to queue-loop.sh (which polls forever and processes
# whatever's ready).
#
# Use cases:
#   1. Demo / debugging — drive one ticket through every stage, watch it.
#   2. Single-stage iteration — repeatedly invoke shepherd on the same ticket
#      after tweaking a prompt, without waiting for the queue.
#   3. "I want this done now" — priority ticket, foreground progress.
#
# Default behavior:
#   - Spawns a tmux window in the existing bureau-v2 session (or a dedicated
#     bureau-shepherd-<slug> session if no bureau session is running), then
#     exits the parent process and prints the attach command. Use --no-tmux
#     to run inline (CI/headless).
#   - Forces every stage on regardless of `.agents.<stage>` toggles via
#     BUREAU_FORCE_ALL_AGENTS=1. Use --respect-config to honor toggles.
#   - Adds `shepherd-focused` label on entry, removes on EXIT/INT/TERM.
#     pipeline_pick_next excludes that label so queue-loop stays out of
#     shepherd's way while a ticket is being driven.
#
# Usage:
#   ./scripts/shepherd.sh EXP-123
#   ./scripts/shepherd.sh --dry-run EXP-123
#   ./scripts/shepherd.sh --no-tmux EXP-123
#   ./scripts/shepherd.sh --no-merge EXP-123
#   ./scripts/shepherd.sh --from-stage build EXP-123
#   ./scripts/shepherd.sh --respect-config EXP-123

set -euo pipefail
unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$(dirname "$0")/bureau-config.sh"

if [ -f .env ]; then
  # shellcheck disable=SC1091
  source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_REPO/.env"
else
  echo "ERROR: No .env found"
  exit 1
fi

# Preserve the original argv so we can re-invoke ourselves inside tmux.
ORIG_ARGS=("$@")

NO_TMUX=0
DRY_RUN=0
NO_MERGE=0
RESPECT_CONFIG=0
FROM_STAGE=""
WORKTREE_OVERRIDE=""
ISSUE=""

print_usage() {
  cat <<'EOF'
Usage: shepherd.sh [flags] ISSUE-KEY

Drives one Linear ticket end-to-end through every pipeline phase.

Flags:
  --dry-run            Print the planned route; do not execute or move state.
  --no-tmux            Run inline in current shell (default: spawn tmux window).
  --no-merge           Halt before the Merge stage even if review approves.
  --from-stage NAME    Move ticket to NAME state first, then start shepherding.
                       NAME ∈ triage|spec_review|design|build|qa|build_review|merge
  --respect-config     Honor .agents.<stage> toggles. Default: force all on.
  --worktree DIR       Build worktree dir (default: .worktrees/shepherd). Use a
                       per-ticket dir (e.g. .worktrees/shepherd-EXP-123) so
                       multiple shepherds can run concurrently without clobbering
                       one another's checkout — the basis of the d&a executor.
  -h, --help           This help.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-tmux)        NO_TMUX=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --no-merge)       NO_MERGE=1; shift ;;
    --respect-config) RESPECT_CONFIG=1; shift ;;
    --from-stage)     FROM_STAGE="${2:-}"; shift 2 ;;
    --from-stage=*)   FROM_STAGE="${1#*=}"; shift ;;
    --worktree)       WORKTREE_OVERRIDE="${2:-}"; shift 2 ;;
    --worktree=*)     WORKTREE_OVERRIDE="${1#*=}"; shift ;;
    -h|--help)        print_usage; exit 0 ;;
    -*)               echo "Unknown flag: $1" >&2; print_usage >&2; exit 1 ;;
    *)
      if [ -z "$ISSUE" ]; then
        ISSUE="$1"; shift
      else
        echo "ERROR: multiple positional args ('$ISSUE', '$1')" >&2; exit 1
      fi
      ;;
  esac
done

if [ -z "$ISSUE" ]; then
  print_usage >&2
  exit 1
fi
if [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
  echo "ERROR: ISSUE must look like 'EXP-123', got: '$ISSUE'" >&2
  exit 1
fi

# ── Tmux wrapper ──────────────────────────────────────────────────────
# Spawn into the existing bureau-v2 session if it's running, else create
# a dedicated bureau-shepherd-<slug> session. Print the attach command and
# exit. Skip when already inside tmux ($TMUX set), running --dry-run, or
# explicitly --no-tmux.
if [ "$NO_TMUX" = 0 ] \
   && [ "$DRY_RUN" = 0 ] \
   && [ -z "${TMUX:-}" ] \
   && command -v tmux >/dev/null 2>&1; then

  REPO_SLUG="$(basename "$REPO_DIR")"
  BUREAU_SESSION="${BUREAU_SESSION_NAME:-bureau-v2-$REPO_SLUG}"
  WINDOW_NAME="shepherd-$ISSUE"

  # Shell-quote each arg so tmux's sh -c re-parsing preserves them exactly.
  # printf %q is available in bash 3.2 (macOS default).
  CMD=$(printf '%q ' "$0" "--no-tmux" "${ORIG_ARGS[@]}")

  if tmux has-session -t "$BUREAU_SESSION" 2>/dev/null; then
    tmux new-window -t "$BUREAU_SESSION:" -c "$REPO_DIR" -n "$WINDOW_NAME" "$CMD"
    TARGET="$BUREAU_SESSION"
  elif tmux has-session -t "bureau-shepherd-$REPO_SLUG" 2>/dev/null; then
    # Fallback session already exists from a prior shepherd run — add a window
    # to it instead of trying to recreate (would collide with `duplicate
    # session`). Lets multiple shepherds run in parallel when bureau-v2-<slug>
    # isn't around.
    TARGET="bureau-shepherd-$REPO_SLUG"
    tmux new-window -t "$TARGET:" -c "$REPO_DIR" -n "$WINDOW_NAME" "$CMD"
  else
    TARGET="bureau-shepherd-$REPO_SLUG"
    tmux new-session -d -s "$TARGET" -c "$REPO_DIR" -n "$WINDOW_NAME" "$CMD"
  fi

  echo "🐑 Shepherd driving $ISSUE in tmux."
  echo "   Session: $TARGET"
  echo "   Window:  $WINDOW_NAME"
  echo ""
  echo "Attach:"
  echo "   tmux a -t $TARGET \\; select-window -t $WINDOW_NAME"
  echo "Or just:"
  echo "   tmux a -t $TARGET"
  exit 0
fi

# ── Inline path: actually drive the ticket ────────────────────────────

# Force-all by default; --respect-config opts out.
if [ "$RESPECT_CONFIG" = 0 ]; then
  export BUREAU_FORCE_ALL_AGENTS=1
fi

precondition_linear
precondition_claude_auth

: "${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

# --from-stage: pre-move the ticket before starting the loop.
if [ -n "$FROM_STAGE" ]; then
  STAGE_UPPER=$(printf '%s' "$FROM_STAGE" | tr '[:lower:]-' '[:upper:]_')
  TARGET_STATE_VAR="BUREAU_STATE_${STAGE_UPPER}"
  TARGET_STATE="${!TARGET_STATE_VAR:-}"
  if [ -z "$TARGET_STATE" ]; then
    echo "ERROR: --from-stage '$FROM_STAGE' has no matching state (looked up \$$TARGET_STATE_VAR)" >&2
    echo "       Valid: triage, spec_review, design, copy, build, qa, build_review, merge" >&2
    exit 1
  fi
  echo "[shepherd] --from-stage $FROM_STAGE → moving $ISSUE first"
  move_issue "$ISSUE" "$TARGET_STATE"
fi

# State (human-readable name from get_issue_state) → pipeline script.
# Returns empty for terminal/unknown states.
state_to_pipeline() {
  case "$1" in
    "Triage")        echo "spec-pipeline.sh" ;;
    "Spec Review")   echo "spec-review-pipeline.sh" ;;
    "Design")        echo "ux-pipeline.sh" ;;
    "Copy")          echo "copy-pipeline.sh" ;;
    "Build")         echo "implement-pipeline.sh" ;;
    "QA")            echo "qa-pipeline.sh" ;;
    "Build Review")  echo "code-review-pipeline.sh" ;;
    "Merge")         echo "merge-pipeline.sh" ;;
    *)               echo "" ;;
  esac
}

# ── Dry run: print the route from current state and exit ──────────────
if [ "$DRY_RUN" = 1 ]; then
  CUR=$(get_issue_state "$ISSUE" 2>/dev/null || echo "")
  echo "═══════════════════════════════════════"
  echo "  Shepherd dry-run: $ISSUE"
  echo "═══════════════════════════════════════"
  echo "  Current state: ${CUR:-unknown}"
  [ "$RESPECT_CONFIG" = 1 ] && echo "  Mode: --respect-config (.agents.<stage> toggles honored)"
  [ "$NO_MERGE" = 1 ]       && echo "  --no-merge: will halt before Merge stage"
  echo ""
  echo "  Forward route from current state (linear walk — actual routing"
  echo "  depends on labels and pipeline verdicts at runtime):"
  STATES=("Triage" "Spec Review" "Design" "Copy" "Build" "QA" "Build Review" "Merge")
  SEEN=0
  for s in "${STATES[@]}"; do
    [ "$s" = "$CUR" ] && SEEN=1
    if [ "$SEEN" = 1 ]; then
      P=$(state_to_pipeline "$s")
      if [ "$NO_MERGE" = 1 ] && [ "$s" = "Merge" ]; then
        echo "    $s → (halt — --no-merge)"
        break
      fi
      echo "    $s → $P"
    fi
  done
  echo "    Done"
  echo "═══════════════════════════════════════"
  exit 0
fi

# ── Claim the ticket; trap to release on any exit path ────────────────
echo "[shepherd] claiming $ISSUE (label: shepherd-focused)"
add_issue_label "$ISSUE" "shepherd-focused" \
  || echo "  WARN: failed to add shepherd-focused label" >&2
trap 'echo "[shepherd] releasing $ISSUE"; remove_issue_label "$ISSUE" "shepherd-focused" 2>/dev/null || true' EXIT INT TERM

# Per-ticket worktree override (d&a executor) — default preserves single-worktree
# serial behavior exactly. `reset_worktree` auto-creates the dir if absent.
WORKTREE="${WORKTREE_OVERRIDE:-$REPO_DIR/.worktrees/shepherd}"
LAST_STATE=""
STUCK_COUNT=0
MAX_STUCK=2

echo ""
echo "═══════════════════════════════════════"
echo "  Shepherd: $ISSUE"
echo "  Force all agents: $([ "$RESPECT_CONFIG" = 0 ] && echo "ON" || echo "OFF (--respect-config)")"
echo "  Tmux: $([ -n "${TMUX:-}" ] && echo "attached" || echo "inline")"
echo "═══════════════════════════════════════"

while true; do
  STATE=$(get_issue_state "$ISSUE" 2>/dev/null || echo "")
  if [ -z "$STATE" ]; then
    echo "[shepherd] WARN: could not read state for $ISSUE (linear transient?) — sleeping 60s"
    sleep 60
    continue
  fi

  echo ""
  echo "[shepherd] $ISSUE @ '$STATE'"

  # Terminal states
  case "$STATE" in
    Done|Cancelled|Canceled|Duplicate)
      echo "[shepherd] terminal state '$STATE' — done"
      exit 0
      ;;
  esac

  # Human-attention guard. The picker (pipeline_pick_next in queue-loop)
  # excludes needs-human / blocked / wip via pick_issue's exclude_csv,
  # so autonomous queue-loop stays away from human-flagged tickets.
  # Shepherd intentionally bypasses the picker
  # (BUREAU_FORCE_ALL_AGENTS=1) to drive a NAMED ticket, which also
  # bypasses that exclusion — we have to repeat the check on the
  # dispatch side or we keep firing pipelines after a stage has
  # already labelled the ticket "stop, human."
  #
  # The existing stuck-detector (STUCK_COUNT >= MAX_STUCK) eventually
  # catches the loop, but only after one wasted pipeline pass at $
  # per Opus call. Fail loud and early instead.
  HUMAN_LABEL_HIT=""
  for forbidden in needs-human blocked wip; do
    if get_issue_detail "$ISSUE" 2>/dev/null \
         | jq -e --arg L "$forbidden" '.labels | index($L)' >/dev/null 2>&1; then
      HUMAN_LABEL_HIT="$forbidden"
      break
    fi
  done
  if [ -n "$HUMAN_LABEL_HIT" ]; then
    echo "[shepherd] '$HUMAN_LABEL_HIT' label present on $ISSUE @ '$STATE' — halting"
    post_comment "$ISSUE" "🐑 Shepherd halt: \`$HUMAN_LABEL_HIT\` label present at \`$STATE\`. The stage that just ran flagged this ticket for human review; shepherd will not re-run it. Remove the label and re-shepherd when ready." || true
    exit 0
  fi

  # --no-merge: halt at Merge boundary
  if [ "$NO_MERGE" = 1 ] && [ "$STATE" = "Merge" ]; then
    echo "[shepherd] reached Merge — halting per --no-merge"
    post_comment "$ISSUE" "🐑 Shepherd halted at Merge per \`--no-merge\`. Merge manually when ready." || true
    exit 0
  fi

  # Stuck detector
  if [ "$STATE" = "$LAST_STATE" ]; then
    STUCK_COUNT=$((STUCK_COUNT + 1))
    if [ "$STUCK_COUNT" -ge "$MAX_STUCK" ]; then
      echo "[shepherd] STUCK at '$STATE' after $MAX_STUCK ticks — labeling needs-human and exiting"
      add_issue_label "$ISSUE" "needs-human" || true
      # Brace-bound the var refs — bash 3.2 (macOS) treats the bytes of
      # multibyte chars like × as part of identifiers, which trips set -u.
      post_comment "$ISSUE" "🛑 Shepherd halt: ran \`$(state_to_pipeline "$STATE")\` ${MAX_STUCK}× but state stayed at \`${STATE}\`. Needs human." || true
      exit 13
    fi
  else
    STUCK_COUNT=0
  fi
  LAST_STATE="$STATE"

  # Auto-bump Spec → Triage (spec-pipeline guards on Triage entry).
  if [ "$STATE" = "Spec" ]; then
    echo "[shepherd] auto-bump Spec → Triage (spec-pipeline only accepts Triage entry)"
    move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE"
    continue
  fi

  PIPELINE=$(state_to_pipeline "$STATE")
  if [ -z "$PIPELINE" ]; then
    echo "[shepherd] no pipeline known for state '$STATE' — labeling needs-human and exiting"
    add_issue_label "$ISSUE" "needs-human" || true
    exit 1
  fi

  BRANCH=$(get_issue_branch "$ISSUE" 2>/dev/null || echo "")
  echo "[shepherd] → $PIPELINE  (branch: ${BRANCH:-<none yet>})"
  # EXP-670 — pause before this (claude-heavy) stage if session usage is near
  # the limit. No-op when no usage signal is available.
  session_throttle_guard
  reset_worktree "$WORKTREE" "$PIPELINE" "${BRANCH:-}"

  set +e
  ( cd "$WORKTREE" && bash "$REPO_DIR/scripts/$PIPELINE" "$ISSUE" )
  RC=$?
  set -e
  CLASS=$(exit_class "$RC")
  echo "[shepherd] $PIPELINE exit=$RC ($CLASS)"

  case "$RC" in
    0|2)
      # Success / queue-empty — re-read state on next iteration.
      ;;
    10|16)
      # Transient: linear-down / claude-unauth. Throttled re-attempt.
      echo "[shepherd] $CLASS — sleeping 60s and retrying"
      sleep 60
      ;;
    11|12|13|14|15|17|18|19)
      echo "[shepherd] $PIPELINE halted ($CLASS) — aborting shepherd"
      alert_telegram "$ISSUE" "$PIPELINE" "$RC" "shepherd halt ($CLASS)" 2>/dev/null || true
      exit "$RC"
      ;;
    *)
      echo "[shepherd] unexpected exit $RC ($CLASS) — aborting"
      exit "$RC"
      ;;
  esac
done
