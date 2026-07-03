#!/bin/bash
# queue-loop-supervised.sh — auto-restart wrapper for queue-loop.sh (EXP-382).
#
# queue-loop.sh has its own infinite while-true loop, so any exit means
# something killed it: OOM, terminal disconnect, unhandled bash error, or a
# panicked subprocess. Without a supervisor the dead tmux pane stays dead.
#
# Behavior:
#   - Re-runs queue-loop.sh with the original args after a crash.
#   - Exponential backoff: 10s → 30s → 60s → 300s (capped at 5 min).
#   - Crash counter resets after BUREAU_SUPERVISOR_STABILITY_WINDOW
#     seconds of clean runtime (default 1h).
#   - After BUREAU_SUPERVISOR_MAX_CRASHES consecutive crashes (default 5),
#     gives up and fires a Telegram alert with the tail of the queue log.
#   - Forwards SIGINT / SIGTERM to the child so Ctrl+C in the tmux pane
#     stops everything cleanly without triggering the restart logic.
#
# Usage (drop-in replacement for queue-loop.sh):
#   ./scripts/queue-loop-supervised.sh implement 30
#   ./scripts/queue-loop-supervised.sh all 15 --dry-run
#
# Opt-out: call queue-loop.sh directly to skip supervision.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
source "$(dirname "$0")/bureau-config.sh"

# Load .env so alert_telegram has TELEGRAM_BOT_TOKEN + TELEGRAM_ALERT_CHAT_ID.
if [ -f "$REPO_DIR/.env" ]; then
  set -a; source "$REPO_DIR/.env"; set +a
fi

MODE_LABEL="${1:-all}"
LOG_DIR="$REPO_DIR/logs"
SUPERVISOR_LOG="$LOG_DIR/supervisor-${MODE_LABEL}.log"
QUEUE_LOG="$LOG_DIR/queue-${MODE_LABEL}.log"
mkdir -p "$LOG_DIR"

MAX_CRASHES="${BUREAU_SUPERVISOR_MAX_CRASHES:-5}"
STABILITY_WINDOW="${BUREAU_SUPERVISOR_STABILITY_WINDOW:-3600}"

# Backoff schedule indexed by crash count - 1. After all entries are exhausted,
# the last value (300s = 5min) is reused for any further crashes within the
# stability window.
BACKOFFS=(10 30 60 300 300)

CRASH_COUNT=0
LAST_CRASH_TS=0
CHILD_PID=""

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$SUPERVISOR_LOG"
}

cleanup() {
  log "Supervisor: shutting down (signal received)"
  if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill -TERM "$CHILD_PID" 2>/dev/null || true
    # Give the child a moment to clean up before we exit; don't block forever.
    for _ in 1 2 3 4 5; do
      kill -0 "$CHILD_PID" 2>/dev/null || break
      sleep 1
    done
  fi
  exit 0
}
trap cleanup INT TERM

log "Supervisor starting: ./scripts/queue-loop.sh $*"
log "Config: max_crashes=$MAX_CRASHES, stability_window=${STABILITY_WINDOW}s"

while true; do
  start_ts=$(date +%s)

  "$REPO_DIR/scripts/queue-loop.sh" "$@" &
  CHILD_PID=$!
  wait "$CHILD_PID"
  child_rc=$?
  CHILD_PID=""

  end_ts=$(date +%s)
  duration=$((end_ts - start_ts))

  # Stability reset: if the child ran cleanly for STABILITY_WINDOW seconds
  # since the last crash, treat any new failure as a fresh incident.
  if [ "$LAST_CRASH_TS" -gt 0 ] \
     && [ $((end_ts - LAST_CRASH_TS)) -gt "$STABILITY_WINDOW" ] \
     && [ "$CRASH_COUNT" -gt 0 ]; then
    log "Supervisor: stable for >${STABILITY_WINDOW}s, resetting crash counter (was $CRASH_COUNT)"
    CRASH_COUNT=0
  fi

  CRASH_COUNT=$((CRASH_COUNT + 1))
  LAST_CRASH_TS=$end_ts

  if [ "$CRASH_COUNT" -ge "$MAX_CRASHES" ]; then
    log "Supervisor: $CRASH_COUNT consecutive crashes — giving up. Last exit: $child_rc, ran ${duration}s."
    tail_log=""
    [ -f "$QUEUE_LOG" ] && tail_log=$(tail -n 30 "$QUEUE_LOG" 2>/dev/null || true)
    alert_telegram "supervisor" "queue-loop-${MODE_LABEL}" "$child_rc" \
      "$CRASH_COUNT consecutive crashes; supervisor giving up. Last duration ${duration}s." \
      "$tail_log" || true
    exit 1
  fi

  # Pick backoff index. Crash 1 → BACKOFFS[0]=10s, crash 2 → 30s, …
  idx=$((CRASH_COUNT - 1))
  [ "$idx" -ge "${#BACKOFFS[@]}" ] && idx=$(( ${#BACKOFFS[@]} - 1 ))
  backoff="${BACKOFFS[$idx]}"

  log "Supervisor: queue-loop crashed (exit $child_rc, ran ${duration}s). Restart $((CRASH_COUNT+1))/$MAX_CRASHES in ${backoff}s."
  sleep "$backoff"
done
