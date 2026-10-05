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
#   - Refuses a ticket a human holds before claiming it: exit 25, nothing
#     written (needs-human or the configured linear.labels.needs_human.name,
#     blocked, wip, or a local hold that mark_needs_human left when it could
#     not write the label). The same check runs on every turn of the loop.
#     A hold left on a finished ticket changes nothing: without --from-stage
#     Done still ends with 0 and a cancelled ticket with 26.
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
  bureau_load_env --export .env
elif [ -f "${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}" ]; then
  bureau_load_env --export "${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
else
  echo "ERROR: No .env found"
  exit 1
fi

# Preserve the original argv so we can re-invoke ourselves inside tmux.
ORIG_ARGS=("$@")

NO_TMUX=0
DRY_RUN="${BUREAU_DRY_RUN:-0}"
NO_MERGE="${BUREAU_NO_MERGE:-0}"
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
                       A ticket a human holds prints the hold, no route, exit 25.
  --no-tmux            Run inline in current shell (default: spawn tmux window).
  --no-merge           Halt before the Merge stage even if review approves.
  --from-stage NAME    Move ticket to NAME state first, then start shepherding.
                       NAME ∈ triage|spec_review|design|build|qa|build_review|merge
  --respect-config     Honor .agents.<stage> toggles. Default: force all on.
  --worktree DIR       Build worktree dir (default: .worktrees/shepherd). Use a
                       per-ticket dir (e.g. .worktrees/shepherd-EXP-123) so
                       multiple shepherds can run concurrently without clobbering
                       one another's checkout — the basis of the d&a executor.
                       A relative DIR is taken from the repo root (the directory
                       the shepherd is started from) and made absolute at once.
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

# A relative --worktree is relative to the repo root, the directory the shepherd
# is started from. It is made absolute here, before it is handed to the runtime
# (--workspace), to bureau-worker.sh and, through ORIG_ARGS, to the tmux window
# and the re-exec under the runtime. Handed on relative, it broke the worker: the
# worker changes into the worktree and its EXIT cleanup ran `git -C <relative>`
# from there — "fatal: cannot change to …", exit 128 after a stage that had
# finished (pilot EXP-1533, rc.1).
if [ -n "$WORKTREE_OVERRIDE" ]; then
  case "$WORKTREE_OVERRIDE" in /*) ;; *) WORKTREE_OVERRIDE="$REPO_DIR/$WORKTREE_OVERRIDE" ;; esac
  _args=(); _next_is_worktree=0
  for _a in "${ORIG_ARGS[@]}"; do
    if [ "$_next_is_worktree" = 1 ]; then _args+=("$WORKTREE_OVERRIDE"); _next_is_worktree=0; continue; fi
    case "$_a" in
      --worktree)   _args+=("$_a"); _next_is_worktree=1 ;;
      --worktree=*) _args+=("--worktree=$WORKTREE_OVERRIDE") ;;
      *)            _args+=("$_a") ;;
    esac
  done
  ORIG_ARGS=("${_args[@]}")
  unset _args _a _next_is_worktree
  # A disposable worker needs a worktree of its own. The main checkout (or the
  # checkout the shepherd runs from) is never reset: the stage would stop with
  # 21 and the halt would ask to drop a checkout nobody may drop (v3.1.0-rc.2).
  _wt_real=$(python3 -I -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$WORKTREE_OVERRIDE")
  _main=$(git -C "$REPO_DIR" worktree list --porcelain 2>/dev/null | sed -n '1s/^worktree //p' || true)
  for _co in "$_main" "$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null || true)"; do
    [ -n "$_co" ] || continue
    if [ "$_wt_real" = "$(python3 -I -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$_co")" ]; then
      echo "ERROR: --worktree $WORKTREE_OVERRIDE is the checkout $_co; the shepherd needs a worktree of its own (e.g. --worktree .worktrees/shepherd-${ISSUE:-TEAM-123})" >&2
      exit 1
    fi
  done
  unset _wt_real _main _co
fi

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

# ── The shepherd's own Linear reads (EXP-1528) ────────────────────────
# Each read is captured first and its exit code decides before anything looks
# at the value. Defaulted with `|| echo ""` or piped straight into jq, a Linear
# that stayed unusable read as "no state" (the loop slept and re-read forever)
# or as "label absent" (the shepherd walked on past needs-human). A read that
# fails is never an empty answer: the caller halts on its code.

# _shepherd_fault_class <file> — the fault class a Linear read left in <file>,
# or "unknown" when there is none or it is not one of the four names. Only such
# a name gets through, so no answer text reaches an alert or a comment.
_shepherd_fault_class() {
  local file="${1:-}" value=""
  [ -n "$file" ] && [ -f "$file" ] && value=$(head -1 "$file" 2>/dev/null | tr -d '\r\n' || true)
  case "$value" in
    no-response | not-json | graphql-errors | no-data) printf '%s' "$value" ;;
    *) printf 'unknown' ;;
  esac
}

# _shepherd_state — the ticket's state name; empty only when Linear answered
# without one. Exit: the read's own code (27 = Linear stayed unusable, and the
# fault class is in $SHEPHERD_FAULT_FILE).
_shepherd_state() {
  _BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" get_issue_state "$ISSUE"
}

# _shepherd_human_label — whether a human holds the ticket (bureau_human_hold in
# bureau-config.sh): "hold <file>" for a local hold (a stage could not write
# needs-human), "label <name>" for the first of needs-human, the configured
# linear.labels.needs_human.name, blocked and wip on the ticket, or nothing. At
# most one read per call, none when a hold file answers. Exit: non-zero when the
# labels could not be read; an answer without a readable label list (nothing at
# all, or no list) fails in jq instead of counting as "no label".
_shepherd_human_label() {
  _BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" bureau_human_hold "$ISSUE"
}

# _shepherd_hold_text <hold> — the operator's line for a hold that line 1 of
# _shepherd_human_label printed: what holds the ticket and how to release it.
_shepherd_hold_text() {
  case "$1" in
    "hold "*) printf '%s is held for a human in %s (a stage could not write the needs-human label); the next queue pick writes the label and ends the hold — to release it without the label, delete that file' "$ISSUE" "${1#hold }" ;;
    *)        printf "%s carries '%s' — a human holds it; remove the label in Linear to release it" "$ISSUE" "${1#label }" ;;
  esac
}

# _shepherd_branch — the ticket's branch (bureau-branch marker, else Linear's
# branchName); empty only when the ticket has none yet (before the spec stage).
# Exit: the read's own code.
_shepherd_branch() {
  _BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" get_issue_branch "$ISSUE"
}

# ── Dry run: print the route from current state and exit ──────────────
if [ "$DRY_RUN" = 1 ]; then
  [ -n "$FROM_STAGE" ] && echo "  [dry-run] requested initial stage: $FROM_STAGE (no state move)"
  # Read-only: a failed read ends the dry run and writes nothing. The trap
  # removes the fault file on every way out, an interrupt included.
  SHEPHERD_FAULT_FILE=$(mktemp "${TMPDIR:-/tmp}/bureau-linear-fault.XXXXXX")
  trap 'rm -f "$SHEPHERD_FAULT_FILE" 2>/dev/null || true' EXIT
  CUR_RC=0
  CUR=$(_shepherd_state) || CUR_RC=$?
  CUR_FAULT=$(_shepherd_fault_class "$SHEPHERD_FAULT_FILE")
  if [ "$CUR_RC" -gt 128 ]; then
    echo "[shepherd] dry-run: interrupted while reading the state of $ISSUE (exit $CUR_RC) — cancelled." >&2
    exit 130
  elif [ "$CUR_RC" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ]; then
    echo "[shepherd] dry-run: could not read the state of $ISSUE — Linear stayed unusable after every retry (fault: $CUR_FAULT). No route printed." >&2
    exit "$CUR_RC"
  elif [ "$CUR_RC" != 0 ]; then
    echo "[shepherd] dry-run: could not read the state of $ISSUE (exit $CUR_RC). No route printed." >&2
    exit 1
  fi
  # The hold check a run makes before its claim (see below), with the same
  # answers: a held ticket prints no route and ends with 25, a failed read ends
  # the dry run like a failed state read. A finished ticket (Done, cancelled)
  # ends a run as it always did, whatever label is left on it, so its labels
  # are not read — unless --from-stage would move it back into the pipeline.
  HOLD=""; HOLD_RC=0
  case "${FROM_STAGE:+moved}$CUR" in
    Done|Cancelled|Canceled|Duplicate) ;;
    *)
      : > "$SHEPHERD_FAULT_FILE" 2>/dev/null || true
      HOLD=$(_shepherd_human_label) || HOLD_RC=$?
      ;;
  esac
  HOLD_FAULT=$(_shepherd_fault_class "$SHEPHERD_FAULT_FILE")
  if [ "$HOLD_RC" -gt 128 ]; then
    echo "[shepherd] dry-run: interrupted while reading the labels of $ISSUE (exit $HOLD_RC) — cancelled." >&2
    exit 130
  elif [ "$HOLD_RC" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ]; then
    echo "[shepherd] dry-run: could not read the labels of $ISSUE — Linear stayed unusable after every retry (fault: $HOLD_FAULT). No route printed." >&2
    exit "$HOLD_RC"
  elif [ "$HOLD_RC" != 0 ]; then
    echo "[shepherd] dry-run: could not read the labels of $ISSUE (exit $HOLD_RC). No route printed." >&2
    exit 1
  fi
  echo "═══════════════════════════════════════"
  echo "  Shepherd dry-run: $ISSUE"
  echo "═══════════════════════════════════════"
  echo "  Current state: ${CUR:-unknown}"
  if [ -n "$HOLD" ]; then
    echo "  Held: $(_shepherd_hold_text "$HOLD")"
    echo "  A run refuses this ticket before claiming it: exit 25, nothing written. No route printed."
    echo "═══════════════════════════════════════"
    exit 25
  fi
  [ "$RESPECT_CONFIG" = 1 ] && echo "  Mode: --respect-config (.agents.<stage> toggles honored)"
  [ "$NO_MERGE" = 1 ]       && echo "  --no-merge: will halt before Merge stage"
  bureau_merge_is_manual    && echo "  agents.merge_mode manual: will halt before Merge stage (a human merges)"
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
      if bureau_merge_is_manual && [ "$s" = "Merge" ]; then
        echo "    $s → (halt — agents.merge_mode manual)"
        break
      fi
      echo "    $s → $P"
    fi
  done
  echo "    Done"
  echo "═══════════════════════════════════════"
  exit 0
fi

# --from-stage is checked before anything is claimed (the runtime's lease, the
# shepherd-focused label): a typo used to be rejected only after claim and release.
TARGET_STATE=""; TARGET_NAME=""
if [ -n "$FROM_STAGE" ]; then
  STAGE_KEY=$(printf '%s' "$FROM_STAGE" | tr '[:upper:]-' '[:lower:]_')
  TARGET_STATE_VAR="BUREAU_STATE_$(printf '%s' "$STAGE_KEY" | tr '[:lower:]' '[:upper:]')"
  TARGET_STATE="${!TARGET_STATE_VAR:-}"
  case "$STAGE_KEY" in
    triage) TARGET_NAME=Triage ;; spec) TARGET_NAME=Spec ;; spec_review) TARGET_NAME='Spec Review' ;;
    design) TARGET_NAME=Design ;; copy) TARGET_NAME=Copy ;; build) TARGET_NAME=Build ;; qa) TARGET_NAME=QA ;;
    build_review) TARGET_NAME='Build Review' ;; merge) TARGET_NAME=Merge ;; done) TARGET_NAME=Done ;;
  esac
  if [ -z "$TARGET_STATE" ] || [ -z "$TARGET_NAME" ]; then
    echo "ERROR: --from-stage '$FROM_STAGE' has no matching state (looked up \$$TARGET_STATE_VAR)" >&2
    echo "       Valid: triage, spec_review, design, copy, build, qa, build_review, merge" >&2
    exit 1
  fi
fi

[ "$NO_MERGE" = 1 ] && export BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=1
WORKTREE="${WORKTREE_OVERRIDE:-$REPO_DIR/.worktrees/shepherd}"
if [ "${BUREAU_ACTIVE_ENTRY:-}" != "$0" ]; then
  bureau_exec_runtime python3 -I "$BUREAU_RUNTIME" --repo "$REPO_DIR" exec --issue "$ISSUE" --workspace "$WORKTREE" --entry "$0" -- bash "$0" --no-tmux "${ORIG_ARGS[@]}"
fi

# The fault class a stage leaves behind when it gives up on Linear (exit
# $BUREAU_EXIT_LINEAR_UNUSABLE). The stage writes only a name from a fixed
# list into this file (_bureau_linear_record in bureau-config.sh); only such a
# name gets through _shepherd_fault_class above, so no answer text reaches an
# alert or a comment. The shepherd's own reads, moves and its start check record
# into the same file. Until the ticket is claimed, leaving only removes the file.
SHEPHERD_FAULT_FILE=$(mktemp "${TMPDIR:-/tmp}/bureau-linear-fault.XXXXXX")
# The merge stage writes the outcome of its gate here (BUREAU_MERGE_GATE_REPORT):
# "not-yet" or "blocked" on the first line, the gate lines after it.
MERGE_GATE_FILE=$(mktemp "${TMPDIR:-/tmp}/bureau-merge-gate.XXXXXX")
trap 'rm -f "$SHEPHERD_FAULT_FILE" "$MERGE_GATE_FILE" 2>/dev/null || true' EXIT

# _shepherd_cancelled <signal> — Ctrl-C, or the runtime forwarding a SIGTERM to
# the process group, ends the run as cancelled: exit 130, the code the runtime
# reports for an interrupted child. The EXIT trap then releases the claim (one
# attempt: the runtime kills the group five seconds after forwarding the
# signal) and nothing else is written — no label, no comment, no alert. The trap
# runs as soon as the command in flight returns, before its caller can take a
# failed read or a failed stage for a finding. Before, INT and TERM only
# released the claim and the loop went on: the next read, or the next stage,
# ran on a ticket nobody held any more.
_shepherd_cancelled() {
  trap - INT TERM
  [ -n "${SHEPHERD_SLEEP_PID:-}" ] && kill "$SHEPHERD_SLEEP_PID" 2>/dev/null || true
  if [ "$SHEPHERD_CLAIMED" = 1 ]; then
    echo "[shepherd] interrupted by $1 — cancelled; nothing written but the release of $ISSUE" >&2
  else
    echo "[shepherd] interrupted by $1 — cancelled before $ISSUE was claimed; nothing written" >&2
  fi
  export _BUREAU_LINEAR_SINGLE_ATTEMPT=1
  exit 130
}
SHEPHERD_CLAIMED=0
trap '_shepherd_cancelled SIGINT' INT
trap '_shepherd_cancelled SIGTERM' TERM

# _shepherd_sleep <seconds> — a wait the traps above can cut short. Bash runs a
# trap only when the foreground command returns, so a SIGTERM sent to the
# shepherd alone during a plain `sleep 60` waited out the minute — and the old
# trap then went on with the loop.
# A signal to the whole process group also kills the sleep: `wait` then returns
# above 128, and under `set -e` that could end the shepherd before the pending
# trap ran (seen on Linux CI). `|| true` leaves the ending to the trap.
SHEPHERD_SLEEP_PID=""
_shepherd_sleep() {
  sleep "$1" &
  SHEPHERD_SLEEP_PID=$!
  wait "$SHEPHERD_SLEEP_PID" || true
  SHEPHERD_SLEEP_PID=""
}

# _shepherd_linear_halt <script> <exit-code> [<doing>] — <script> gave up because
# Linear stayed unusable after every retry (while <doing> — "reading the state",
# "moving the ticket to Triage" — when the shepherd itself made the call).
# Nothing was decided on the empty answer, so the halt is
# ours to make visible: alert first (Telegram does not need Linear), then label
# and comment with a SINGLE attempt each — Linear just failed every retry, and
# another full ladder per write would only delay the halt.
_shepherd_linear_halt() {
  local script="$1" rc="$2" doing="${3:+ $3}" class fault
  class=$(exit_class "$rc")
  fault=$(_shepherd_fault_class "$SHEPHERD_FAULT_FILE")
  echo "[shepherd] $script$doing halted ($class, fault: $fault) — labeling needs-human and aborting shepherd"
  alert_telegram "$ISSUE" "$script" "$rc" "shepherd halt ($class: $fault)$doing" 2>/dev/null || true
  export _BUREAU_LINEAR_SINGLE_ATTEMPT=1
  add_issue_label "$ISSUE" "needs-human" \
    || echo "[shepherd] WARN: could not add the 'needs-human' label to $ISSUE — Linear is still unusable" >&2
  post_comment "$ISSUE" "🛑 Shepherd halt: \`$script\`$doing gave up because Linear stayed unusable after every retry (\`$fault\`). Nothing was decided on the empty answer. Needs human — re-shepherd once Linear answers again." \
    || echo "[shepherd] WARN: could not post the halt comment on $ISSUE — Linear is still unusable" >&2
  exit "$rc"
}

# _shepherd_read_failed <what> <exit-code> — the shepherd's own read of <what>
# failed, so it cannot tell which stage runs next or whether a human holds the
# ticket. A read that ended by a signal only its own process saw (a code above
# 128) is a cancelled run like _shepherd_cancelled: exit 130, nothing written —
# the operator stopped it, the ticket is not at fault. 27 takes the Linear halt
# above. Any other code (a usable answer the helper could not parse, a helper
# missing from an older config) says nothing about the ticket either: halt with
# 1, label needs-human and say which read failed — walking on or waiting would
# decide on an answer nobody read.
_shepherd_read_failed() {
  local what="$1" rc="$2"
  if [ "$rc" -gt 128 ]; then
    echo "[shepherd] interrupted while reading the $what of $ISSUE (exit $rc) — cancelled, nothing written" >&2
    exit 130
  fi
  [ "$rc" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ] && _shepherd_linear_halt shepherd.sh "$rc" "reading the $what"
  echo "[shepherd] could not read the $what of $ISSUE (exit $rc) — labeling needs-human and aborting shepherd" >&2
  alert_telegram "$ISSUE" shepherd.sh "$rc" "shepherd halt (could not read the $what, exit $rc)" 2>/dev/null || true
  add_issue_label "$ISSUE" "needs-human" \
    || echo "[shepherd] WARN: could not add the 'needs-human' label to $ISSUE" >&2
  post_comment "$ISSUE" "🛑 Shepherd halt: could not read the $what of this ticket (exit $rc). Nothing was decided without it. Needs human — re-shepherd once the read works again." \
    || echo "[shepherd] WARN: could not post the halt comment on $ISSUE" >&2
  exit 1
}

# _shepherd_move_failed <state> <exit-code> — the shepherd's own move of the
# ticket to <state> (--from-stage, or the Spec → Triage bump) failed (EXP-1482).
# Called bare under `set -e`, a failed move used to end the shepherd with the
# move's code before any halt handling: no alert, no label, no comment. The
# same three ways out as a failed read: a signal only the move saw → cancelled
# (130, nothing written); 27 → the Linear halt; any other code → halt with 1,
# needs-human and a comment — the ticket may or may not have moved, and the
# next stage must not start on a guess.
_shepherd_move_failed() {
  local target="$1" rc="$2"
  if [ "$rc" -gt 128 ]; then
    echo "[shepherd] interrupted while moving $ISSUE to $target (exit $rc) — cancelled, nothing written" >&2
    exit 130
  fi
  [ "$rc" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ] && _shepherd_linear_halt shepherd.sh "$rc" "moving the ticket to $target"
  echo "[shepherd] could not move $ISSUE to $target (exit $rc) — labeling needs-human and aborting shepherd" >&2
  alert_telegram "$ISSUE" shepherd.sh "$rc" "shepherd halt (could not move the ticket to $target, exit $rc)" 2>/dev/null || true
  add_issue_label "$ISSUE" "needs-human" \
    || echo "[shepherd] WARN: could not add the 'needs-human' label to $ISSUE" >&2
  post_comment "$ISSUE" "🛑 Shepherd halt: could not move this ticket to \`$target\` (exit $rc). It may or may not have moved; no stage was started on a guess. Needs human — check the state and re-shepherd." \
    || echo "[shepherd] WARN: could not post the halt comment on $ISSUE" >&2
  exit 1
}

# Start check (EXP-1482). It runs before the claim, so a failure has touched
# nothing on the ticket: no needs-human (it would keep the queue away from a
# ticket that is fine, and it needs the Linear that just failed) and no comment.
# It used to end inside precondition_linear with a bare exit 10 and a message
# about the key, whatever the cause, and nobody was told. Now it keeps the
# documented code (10, linear-down — the contract with the callers), names the
# fault class the fetch recorded when it gave up, and alerts: an orchestrated
# chain stops its lane on this code without telling anyone.
_shepherd_start_failed() {
  local rc="$1" fault
  [ "$rc" -gt 128 ] && _shepherd_cancelled "a signal (exit $rc)"
  fault=$(_shepherd_fault_class "$SHEPHERD_FAULT_FILE")
  echo "[shepherd] Linear start check failed ($(exit_class "$rc"), fault: $fault) — $ISSUE not claimed, nothing written" >&2
  alert_telegram "$ISSUE" shepherd.sh "$rc" "shepherd did not start ($(exit_class "$rc"): $fault)" 2>/dev/null || true
  exit "$rc"
}

( _BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" precondition_linear ) || _shepherd_start_failed $?

: "${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

# ── Refuse a held ticket before claiming it (v3.1) ────────────────────
# The loop below reads the labels on every turn, but only after the claim and
# the --from-stage move: a held ticket got shepherd-focused and was moved before
# the shepherd saw the hold, a configured needs-human name was not looked at,
# and a local hold (the label could not be written, mark_needs_human) was not
# read at all. Now a held ticket ends here with 25 and nothing written: no
# claim, no move, no label, no comment, no alert. The line on stderr names the
# hold and how to release it. A hold left on a ticket that is already finished
# (a human closed a ticket the shepherd had halted on) changes nothing: without
# --from-stage such a ticket ends the run as the loop ends it, Done with 0 and a
# cancelled one with 26, still without a claim or a write. The state is read only
# on that path, so a free ticket costs no extra read. With --from-stage the
# ticket would be moved back into the pipeline, so its hold refuses it. A read that fails ends like the start
# check: nothing written, an alert, and 27 when Linear stayed unusable, 130 for
# a signal, 1 for anything else — never "no hold".
# _shepherd_hold_check_failed <exit-code> [<what>] — <what> is the read that
# failed (default: labels).
_shepherd_hold_check_failed() {
  local rc="$1" what="${2:-labels}" fault
  [ "$rc" -gt 128 ] && _shepherd_cancelled "a signal (exit $rc)"
  fault=$(_shepherd_fault_class "$SHEPHERD_FAULT_FILE")
  if [ "$rc" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ]; then
    echo "[shepherd] could not read the $what of $ISSUE — Linear stayed unusable after every retry (fault: $fault); $ISSUE not claimed, nothing written" >&2
    alert_telegram "$ISSUE" shepherd.sh "$rc" "shepherd did not start (could not read the $what, $(exit_class "$rc"): $fault)" 2>/dev/null || true
    exit "$rc"
  fi
  echo "[shepherd] could not read the $what of $ISSUE (exit $rc); $ISSUE not claimed, nothing written" >&2
  alert_telegram "$ISSUE" shepherd.sh 1 "shepherd did not start (could not read the $what, exit $rc)" 2>/dev/null || true
  exit 1
}
: > "$SHEPHERD_FAULT_FILE" 2>/dev/null || true
HOLD_RC=0
HOLD=$(_shepherd_human_label) || HOLD_RC=$?
[ "$HOLD_RC" = 0 ] || _shepherd_hold_check_failed "$HOLD_RC"
if [ -n "$HOLD" ]; then
  HELD_STATE=""
  if [ -z "$FROM_STAGE" ]; then
    : > "$SHEPHERD_FAULT_FILE" 2>/dev/null || true
    HELD_STATE_RC=0
    HELD_STATE=$(_shepherd_state) || HELD_STATE_RC=$?
    [ "$HELD_STATE_RC" = 0 ] || _shepherd_hold_check_failed "$HELD_STATE_RC" state
  fi
  case "$HELD_STATE" in
    Done)
      echo "[shepherd] terminal state 'Done' — done (the hold left on it stays: $HOLD; nothing claimed, nothing written)"
      exit 0 ;;
    Cancelled|Canceled|Duplicate)
      echo "[shepherd] cancelled: $HELD_STATE (the hold left on it stays: $HOLD; nothing claimed, nothing written)"
      exit 26 ;;
  esac
  echo "[shepherd] $(_shepherd_hold_text "$HOLD") — not claimed, nothing written (exit 25)" >&2
  exit 25
fi

# ── Claim the ticket; the trap releases it on any exit path ───────────
# The trap is set before the claim: a signal during the claim still releases.
# Any exit above 128 (a signal, whichever way it ended the shell) releases with
# one attempt, like a cancelled run.
trap '[ $? -gt 128 ] && export _BUREAU_LINEAR_SINGLE_ATTEMPT=1; echo "[shepherd] releasing $ISSUE"; remove_issue_label "$ISSUE" "shepherd-focused" 2>/dev/null || true; rm -f "$SHEPHERD_FAULT_FILE" "$MERGE_GATE_FILE" 2>/dev/null || true' EXIT
SHEPHERD_CLAIMED=1
echo "[shepherd] claiming $ISSUE (label: shepherd-focused)"
add_issue_label "$ISSUE" "shepherd-focused" \
  || echo "  WARN: failed to add shepherd-focused label" >&2

# --from-stage: move the ticket before the loop. After the claim (it used to
# run before it), so the queue keeps away from a ticket that just moved into a
# stage's waiting room.
MOVED_TO=""
if [ -n "$FROM_STAGE" ]; then
  echo "[shepherd] --from-stage $FROM_STAGE → moving $ISSUE first"
  : > "$SHEPHERD_FAULT_FILE" 2>/dev/null || true
  _BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" move_issue "$ISSUE" "$TARGET_STATE" \
    || _shepherd_move_failed "$FROM_STAGE" $?
  MOVED_TO="$TARGET_NAME"
fi

# Per-ticket worktree override (d&a executor) — default preserves single-worktree
# serial behavior exactly. `reset_worktree` auto-creates the dir if absent.
WORKTREE="${WORKTREE_OVERRIDE:-$REPO_DIR/.worktrees/shepherd}"
LAST_STATE=""
STUCK_COUNT=0
MAX_STUCK=2
# Linear answering without a state (an unknown or hidden ticket, not a failed
# read — those end above) used to be retried every 60 s without end. The fifth
# such answer in a row halts (EXP-1482 handover).
NO_STATE_COUNT=0
MAX_NO_STATE=5
# Confirming a move (EXP-1482 path 3). A read right after a move may be a
# moment old — a second start of the same stage came from exactly that
# (EXP-1476, 17.09.2026). --from-stage knows only where it went (MOVED_TO: the
# state before it is never read); the bump to Triage and a stage that returned
# 0 know where the ticket was (MOVED_FROM). A read that does not show MOVED_TO,
# or still shows MOVED_FROM, is read again, up to CONFIRM_TRIES times,
# CONFIRM_SECONDS apart, before the shepherd acts on it. A read that already
# shows the move costs nothing extra; a ticket that really stayed where it was
# reaches the stuck detector as before, 15 s later.
# A stage that moves twice leaves a third state a read may show: spec-pipeline.sh
# moves Triage → Spec at its start and Spec → Spec Review at its end, and a read a
# moment old shows the Spec in between (MOVED_VIA). That read is not the state the
# stage started from, so it used to count as confirmed, and the bump below sent the
# finished spec back to Triage — the actual sequence of EXP-1476 (17.09.2026) and
# again of EXP-1554 (05.10.2026). MOVED_VIA is read again like MOVED_FROM.
MOVED_FROM=""
MOVED_VIA=""
CONFIRM_TRIES=3
CONFIRM_SECONDS="${BUREAU_SHEPHERD_CONFIRM_SECONDS:-5}"
case "$CONFIRM_SECONDS" in
  '' | *[!0-9]*)
    echo "[shepherd] WARN: BUREAU_SHEPHERD_CONFIRM_SECONDS='$CONFIRM_SECONDS' is not a whole number of seconds — using 5" >&2
    CONFIRM_SECONDS=5 ;;
esac
_shepherd_unconfirmed() {
  { [ -n "$MOVED_TO" ] && [ "$STATE" != "$MOVED_TO" ]; } || { [ -n "$MOVED_FROM" ] && [ "$STATE" = "$MOVED_FROM" ]; } \
    || { [ -n "$MOVED_VIA" ] && [ "$STATE" = "$MOVED_VIA" ]; }
}
# Waiting on the merge gate (v3.0.1). A merge stage that did not merge used to
# end with 0: the shepherd then took the unchanged Merge state for a move it had
# not seen yet ("still reads 'Merge' after the move"), ran the stage again and
# the stuck detector labeled the ticket after two passes — also while the checks
# were merely running. Now the stage reports its gate: "not yet" (checks pending,
# GitHub still computing) is waited for, BUREAU_SHEPHERD_MERGE_POLL_SECONDS
# apart, up to BUREAU_SHEPHERD_MERGE_WAIT_SECONDS in total, without counting as
# stuck; "blocked", or a gate still not eligible when the wait is used up, halts
# with needs-human and the gate report. Nothing merges on a red or pending gate.
# _shepherd_seconds <name> <default> <min> <max> — the value of <name> in whole
# seconds, read in base 10 (a leading zero is no octal number: "08" is 8, not a
# syntax error), within <min>..<max>. Anything else warns and uses the default;
# a value above <max> uses <max>.
_shepherd_seconds() {
  local name="$1" default="$2" min="$3" max="$4" raw digits
  raw="${!name:-}"
  [ -n "$raw" ] || { echo "$default"; return; }
  case "$raw" in
    *[!0-9]*) echo "[shepherd] WARN: $name='$raw' is not a whole number of seconds — using $default" >&2; echo "$default"; return ;;
  esac
  digits="${raw#"${raw%%[!0]*}"}"; digits="${digits:-0}"
  if [ "${#digits}" -gt 6 ] || [ "$((10#$digits))" -gt "$max" ]; then
    echo "[shepherd] WARN: $name='$raw' is above $max seconds — using $max" >&2; echo "$max"; return
  fi
  if [ "$((10#$digits))" -lt "$min" ]; then
    echo "[shepherd] WARN: $name='$raw' is below $min seconds — using $default" >&2; echo "$default"; return
  fi
  echo "$((10#$digits))"
}
MERGE_WAIT_SECONDS=$(_shepherd_seconds BUREAU_SHEPHERD_MERGE_WAIT_SECONDS 1800 0 21600)
MERGE_POLL_SECONDS=$(_shepherd_seconds BUREAU_SHEPHERD_MERGE_POLL_SECONDS 60 1 3600)
MERGE_WAITED=0
MERGE_WAIT_FOR=""

# _shepherd_no_state_halt — Linear answered MAX_NO_STATE times in a row, without
# an error and without a state. Nothing tells which stage runs next; the same
# halt as a state no pipeline knows: needs-human, a comment, an alert, exit 1.
# _shepherd_merge_blocked <what> — the merge gate decided against the merge, or
# never became eligible within the wait: label, comment with the gate report,
# alert, exit 25. Nothing was merged.
_shepherd_merge_blocked() {
  local what="$1" report
  report=$(sed -n '2,$p' "$MERGE_GATE_FILE" 2>/dev/null | sed -e '/^$/d' -e 's/^/- /')
  echo "[shepherd] merge gate $what for $ISSUE — labeling needs-human and aborting shepherd" >&2
  alert_telegram "$ISSUE" "$PIPELINE" 25 "shepherd halt (merge gate $what)" 2>/dev/null || true
  add_issue_label "$ISSUE" "needs-human" \
    || echo "[shepherd] WARN: could not add the 'needs-human' label to $ISSUE" >&2
  post_comment "$ISSUE" "🛑 Shepherd halt at $STATE: the merge gate $what. Nothing was merged.

${report:-- (the merge stage left no gate report)}

Clear the blocker (or re-run the checks), remove needs-human and re-shepherd." \
    || echo "[shepherd] WARN: could not post the halt comment on $ISSUE" >&2
  exit 25
}

_shepherd_no_state_halt() {
  echo "[shepherd] Linear answered $MAX_NO_STATE times without a state for $ISSUE — labeling needs-human and aborting shepherd" >&2
  alert_telegram "$ISSUE" shepherd.sh 1 "shepherd halt (no state in $MAX_NO_STATE answers)" 2>/dev/null || true
  add_issue_label "$ISSUE" "needs-human" \
    || echo "[shepherd] WARN: could not add the 'needs-human' label to $ISSUE" >&2
  post_comment "$ISSUE" "🛑 Shepherd halt: Linear answered $MAX_NO_STATE times in a row without a state for this ticket. Nothing was started. Needs human — check that the ticket exists and this key can see it, then re-shepherd." \
    || echo "[shepherd] WARN: could not post the halt comment on $ISSUE" >&2
  exit 1
}

echo ""
echo "═══════════════════════════════════════"
echo "  Shepherd: $ISSUE"
echo "  Force all agents: $([ "$RESPECT_CONFIG" = 0 ] && echo "ON" || echo "OFF (--respect-config)")"
echo "  Tmux: $([ -n "${TMUX:-}" ] && echo "attached" || echo "inline")"
echo "═══════════════════════════════════════"

while true; do
  if bureau_is_paused; then echo "[shepherd] paused"; exit 25; fi
  STATE=$(_shepherd_state) || _shepherd_read_failed state $?
  if [ -z "$STATE" ]; then
    # Linear answered, but without a state (transient faults are retried inside
    # the read and end in 27 above).
    NO_STATE_COUNT=$((NO_STATE_COUNT + 1))
    [ "$NO_STATE_COUNT" -ge "$MAX_NO_STATE" ] && _shepherd_no_state_halt
    echo "[shepherd] WARN: Linear answered without a state for $ISSUE — sleeping 60s ($NO_STATE_COUNT/$MAX_NO_STATE)"
    _shepherd_sleep 60
    continue
  fi
  NO_STATE_COUNT=0

  # Confirm a move before acting on it (see MOVED_TO / MOVED_FROM / MOVED_VIA above).
  CONFIRM_COUNT=0
  while _shepherd_unconfirmed && [ "$CONFIRM_COUNT" -lt "$CONFIRM_TRIES" ]; do
    CONFIRM_COUNT=$((CONFIRM_COUNT + 1))
    echo "[shepherd] $ISSUE still reads '$STATE' after the move — reading again in ${CONFIRM_SECONDS}s ($CONFIRM_COUNT/$CONFIRM_TRIES)"
    _shepherd_sleep "$CONFIRM_SECONDS"
    STATE=$(_shepherd_state) || _shepherd_read_failed state $?
  done
  MOVED_FROM=""; MOVED_TO=""; MOVED_VIA=""
  # An answer without a state while confirming goes through the check above.
  [ -z "$STATE" ] && continue

  echo ""
  echo "[shepherd] $ISSUE @ '$STATE'"

  # Terminal states
  case "$STATE" in
    Done)
      echo "[shepherd] terminal state '$STATE' — done"
      exit 0
      ;;
  esac

  case "$STATE" in Cancelled|Canceled|Duplicate) echo "[shepherd] cancelled: $STATE"; exit 26 ;; esac

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
  # per Opus call. Fail loud and early instead — and a label list that could
  # not be read halts too, it never counts as "no label" (EXP-1528).
  HUMAN_HOLD=$(_shepherd_human_label) || _shepherd_read_failed labels $?
  if [ "${HUMAN_HOLD%% *}" = hold ]; then
    echo "[shepherd] $(_shepherd_hold_text "$HUMAN_HOLD") @ '$STATE' — halting"
    post_comment "$ISSUE" "🐑 Shepherd halt at \`$STATE\`: this ticket is held for a human, but its needs-human label could not be written, so the hold is kept locally. Shepherd will not re-run it. The next queue pick writes the label; remove it and re-shepherd when ready." || true
    exit 25
  fi
  HUMAN_LABEL_HIT="${HUMAN_HOLD#label }"
  if [ -n "$HUMAN_LABEL_HIT" ]; then
    echo "[shepherd] '$HUMAN_LABEL_HIT' label present on $ISSUE @ '$STATE' — halting"
    post_comment "$ISSUE" "🐑 Shepherd halt: \`$HUMAN_LABEL_HIT\` label present at \`$STATE\`. The stage that just ran flagged this ticket for human review; shepherd will not re-run it. Remove the label and re-shepherd when ready." || true
    exit 25
  fi

  # --no-merge: halt at Merge boundary
  if [ "$NO_MERGE" = 1 ] && [ "$STATE" = "Merge" ]; then
    echo "[shepherd] reached Merge — halting per --no-merge"
    post_comment "$ISSUE" "🐑 Shepherd halted at Merge per \`--no-merge\`. Merge manually when ready." || true
    exit 20
  fi
  # agents.merge_mode = manual: the same boundary, set by the repo instead of
  # the caller. The expected end of the automated run — no alert, no label.
  if bureau_merge_is_manual && [ "$STATE" = "Merge" ]; then
    echo "[shepherd] reached Merge — halting, agents.merge_mode is manual (a human merges)"
    post_comment "$ISSUE" "🐑 Shepherd halted at Merge: \`agents.merge_mode\` is manual, so a human merges the PR." || true
    exit 20
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

  # Auto-bump Spec → Triage (spec-pipeline guards on Triage entry). It moves the
  # ticket back, so a Spec read right after the spec stage was confirmed above.
  if [ "$STATE" = "Spec" ]; then
    echo "[shepherd] auto-bump Spec → Triage (spec-pipeline only accepts Triage entry)"
    : > "$SHEPHERD_FAULT_FILE" 2>/dev/null || true
    _BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE" \
      || _shepherd_move_failed Triage $?
    MOVED_FROM="$STATE"
    continue
  fi

  PIPELINE=$(state_to_pipeline "$STATE")
  if [ -z "$PIPELINE" ]; then
    echo "[shepherd] no pipeline known for state '$STATE' — labeling needs-human and exiting"
    add_issue_label "$ISSUE" "needs-human" || true
    exit 1
  fi

  BRANCH=$(_shepherd_branch) || _shepherd_read_failed branch $?
  echo "[shepherd] → $PIPELINE  (branch: ${BRANCH:-<none yet>})"
  # EXP-670 — pause before this (claude-heavy) stage if session usage is near
  # the limit. No-op when no usage signal is available.
  case "$PIPELINE" in merge-pipeline.sh|rebase-pipeline.sh) ;; *)
    session_throttle_guard "$(printf '%s' "${PIPELINE%-pipeline.sh}" | tr '-' '_')" ;;
  esac

  # The wait budget belongs to the gate of one stage: it runs on while that stage runs
  # again, and starts at 0 for any other stage (MERGE_WAIT_FOR is set by the wait below).
  if [ "$PIPELINE" != "$MERGE_WAIT_FOR" ]; then MERGE_WAITED=0; MERGE_WAIT_FOR=""; fi
  set +e
  : > "$SHEPHERD_FAULT_FILE" 2>/dev/null || true
  : > "$MERGE_GATE_FILE" 2>/dev/null || true
  # BUREAU_HELD_BY_SHEPHERD tells the stage that this ticket is held here: the
  # queue skips it (shepherd-focused), so nothing but this loop runs a stage on it.
  ( cd "$REPO_DIR" && _BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" BUREAU_MERGE_GATE_REPORT="$MERGE_GATE_FILE" BUREAU_HELD_BY_SHEPHERD=1 \
      bash "$SCRIPT_REPO/scripts/bureau-worker.sh" "$ISSUE" "$PIPELINE" "$WORKTREE" "${BRANCH:-}" )
  RC=$?
  set -e
  CLASS=$(exit_class "$RC")
  echo "[shepherd] $PIPELINE exit=$RC ($CLASS)"

  # The merge stage's gate (see MERGE_WAIT_SECONDS above). Only its own report
  # counts: a 2 or 25 without one takes the general handling below. The review
  # stage's inline merge (agents.merge off) hands on the same report: its "not
  # yet" is waited for the same way (the next review run reuses the recorded
  # approval and runs only the build check and the gate); its "blocked" has
  # already set needs-human and commented, so it halts through the general
  # handling (alert, 25).
  if [ "$PIPELINE" = merge-pipeline.sh ] || [ "$PIPELINE" = code-review-pipeline.sh ]; then
    GATE_OUTCOME=$(head -n 1 "$MERGE_GATE_FILE" 2>/dev/null || true)
    [ "$PIPELINE" = merge-pipeline.sh ] || [ "$GATE_OUTCOME" = not-yet ] || GATE_OUTCOME=""
    case "$RC:$GATE_OUTCOME" in
      2:not-yet)
        [ "$MERGE_WAITED" -ge "$MERGE_WAIT_SECONDS" ] \
          && _shepherd_merge_blocked "was still not eligible after ${MERGE_WAITED}s"
        echo "[shepherd] merge gate not yet eligible — waiting ${MERGE_POLL_SECONDS}s (${MERGE_WAITED}/${MERGE_WAIT_SECONDS}s): $(sed -n 2p "$MERGE_GATE_FILE")"
        _shepherd_sleep "$MERGE_POLL_SECONDS"
        MERGE_WAITED=$((MERGE_WAITED + MERGE_POLL_SECONDS))
        MERGE_WAIT_FOR="$PIPELINE"
        LAST_STATE=""   # waiting for the gate is not a stage that failed to move
        continue
        ;;
      25:blocked)
        _shepherd_merge_blocked "is blocked"
        ;;
    esac
  fi

  # Halt is the default for every code but the four listed in
  # shepherd_rc_action; a code added later cannot slip through unannounced.
  ACTION=$(shepherd_rc_action "$RC")
  [ "$ACTION" = halt ] && [ "$RC" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ] && ACTION=linear-halt
  # A review that stopped before merge because the caller asked for it
  # (--no-merge) ends the run as requested: no alert, no label.
  if [ "$ACTION" = halt ] && stop_before_merge_was_asked "$RC"; then ACTION=stopped-before-merge; fi
  case "$ACTION" in
    ok)
      # Success / queue-empty — re-read state on next iteration, and confirm
      # the stage's move there before starting the next stage.
      MOVED_FROM="$STATE"
      case "$RC:$PIPELINE" in 0:spec-pipeline.sh) MOVED_VIA="Spec" ;; esac
      ;;
    retry)
      # Transient: linear-down / provider-unauth. Throttled re-attempt.
      echo "[shepherd] $CLASS — sleeping 60s and retrying"
      _shepherd_sleep 60
      ;;
    stopped-before-merge)
      echo "[shepherd] $PIPELINE stopped before merge, as asked — a human merges"
      exit "$RC"
      ;;
    linear-halt)
      # The stage gave up because Linear stayed unusable after every retry.
      _shepherd_linear_halt "$PIPELINE" "$RC"
      ;;
    *)
      echo "[shepherd] $PIPELINE halted ($CLASS) — aborting shepherd"
      alert_telegram "$ISSUE" "$PIPELINE" "$RC" "shepherd halt ($CLASS)" 2>/dev/null || true
      exit "$RC"
      ;;
  esac
done
