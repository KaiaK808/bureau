#!/bin/bash
# orchestrate.sh — the bureau-workflow EXECUTOR (PR #137 Decision D).
#
# Consumes a conflict-aware schedule (from the `conflict-aware-schedule`
# workflow brain) and drives `shepherd.sh` over it. The brain decides WHAT can
# run together; this script EXECUTES that decision durably in bash — the
# load-bearing split from PR #137 (planning in a workflow, execution in
# cron-friendly bash).
#
# Usage:
#   scripts/orchestrate.sh --chain EXP-644,EXP-640,EXP-645,EXP-473,EXP-474
#       Run an explicit serial chain, one shepherd at a time, in order.
#   scripts/orchestrate.sh --schedule path/to/schedule.json
#       Read {"serialChains":[[...]],"parallelSafe":[...]} (the brain's output)
#       and run it SERIALLY (legacy default — parallelSafe flattened to chains).
#   scripts/orchestrate.sh --execute --schedule path/to/schedule.json
#       The d&a EXECUTOR: run lanes CONCURRENTLY in per-ticket worktrees,
#       capped at --max-concurrent (default 3). Each serialChain is one lane
#       (serial within); each parallelSafe ticket is its own lane.
#
# Flags:
#   --execute          Concurrent lane execution (per-ticket worktrees). Without
#                      it, behaviour is the legacy serial path (unchanged).
#   --max-concurrent N Lane concurrency cap in --execute mode (default 3, or
#                      $BUREAU_MAX_CONCURRENT).
#   --no-merge         pass through to shepherd (halt before Merge)
#   --dry-run          print the plan + the shepherd commands; mutate nothing
#
# Behaviour:
#   * serialChains: each chain runs strictly in order; a ticket must reach a
#     terminal Linear state (Done) before the next starts. A non-zero shepherd
#     exit STOPS that chain/lane and is reported loudly — never silently skipped.
#   * --execute: lanes run in background, ≤ MAX_CONCURRENT at a time, each in its
#     own worktree `.worktrees/shepherd-lane-<i>` (via `shepherd.sh --worktree`),
#     so two builds never share a checkout. The conflict-aware-schedule brain is
#     trusted to keep main.rs-colliding tickets OUT of the parallelSafe set
#     (EXP-515 widens that frontier later; the executor needs no change for it).
#     Spawned shepherds inherit BUREAU_RUNNER_*/BUREAU_CODEX_MODEL_* env, so
#     concurrent lanes can run qa/code_review on Codex (off the Claude quota).
#
# Exit codes mirror bureau-config.sh::exit_class for the FIRST failing ticket;
# 0 only if every scheduled ticket reached Done.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Test seam: tests inject a stub shepherd via $SHEPHERD_BIN to exercise the
# executor's concurrency / ordering / honest-exit without a real pipeline.
SHEPHERD="${SHEPHERD_BIN:-$REPO_DIR/scripts/shepherd.sh}"

CHAIN_CSV=""
SCHEDULE_FILE=""
PASSTHRU=()
DRY_RUN=0
EXECUTE=0
MAX_CONCURRENT="${BUREAU_MAX_CONCURRENT:-3}"

while [ $# -gt 0 ]; do
  case "$1" in
    --chain)          CHAIN_CSV="$2"; shift 2 ;;
    --schedule)       SCHEDULE_FILE="$2"; shift 2 ;;
    --execute)        EXECUTE=1; shift ;;
    --max-concurrent) MAX_CONCURRENT="$2"; shift 2 ;;
    --no-merge)       PASSTHRU+=("--no-merge"); shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    *) echo "orchestrate: unknown arg '$1'" >&2; exit 2 ;;
  esac
done

if ! [ "$MAX_CONCURRENT" -ge 1 ] 2>/dev/null; then
  echo "orchestrate: --max-concurrent must be a positive integer (got '$MAX_CONCURRENT')" >&2
  exit 2
fi

# A "lane" is a comma-joined ticket set that runs serially within itself; lanes
# run concurrently in --execute mode. In legacy mode every lane is just a serial
# chain run one-at-a-time in the shared worktree.
declare -a LANES=()
if [ -n "$CHAIN_CSV" ]; then
  LANES+=("$CHAIN_CSV")
elif [ -n "$SCHEDULE_FILE" ]; then
  if ! command -v jq >/dev/null 2>&1; then
    echo "orchestrate: jq required to read --schedule" >&2; exit 2
  fi
  # Each serialChain → one lane.
  while IFS= read -r line; do
    [ -n "$line" ] && LANES+=("$line")
  done < <(jq -r '.serialChains[]? | join(",")' "$SCHEDULE_FILE")
  # parallelSafe → one lane PER ticket.
  PSAFE="$(jq -r '.parallelSafe[]? ' "$SCHEDULE_FILE" 2>/dev/null)"
  if [ -n "$PSAFE" ]; then
    if [ "$EXECUTE" = "1" ]; then
      while IFS= read -r t; do
        [ -n "$t" ] && LANES+=("$t")
      done <<< "$PSAFE"
    else
      # Legacy: no per-worktree concurrency → run them serially, logged loudly
      # (no silent cap — see PR #137 §"No silent caps"). Use --execute for real
      # concurrency.
      echo "orchestrate: NOTE — parallelSafe set present; running SERIALLY"
      echo "             (pass --execute for concurrent per-worktree lanes):"
      while IFS= read -r t; do
        [ -n "$t" ] && { echo "               - $t"; LANES+=("$t"); }
      done <<< "$PSAFE"
    fi
  fi
else
  echo "orchestrate: need --chain CSV or --schedule FILE" >&2; exit 2
fi

echo "═══════════════════════════════════════"
echo "  orchestrate — bureau-workflow executor"
echo "  lanes: ${#LANES[@]}"
if [ "$EXECUTE" = "1" ]; then
  echo "  mode: EXECUTE (concurrent, cap ${MAX_CONCURRENT})"
else
  echo "  mode: serial (legacy; --execute for concurrency)"
fi
[ "$DRY_RUN" = "1" ] && echo "  DRY-RUN: no shepherds will be launched"
echo "═══════════════════════════════════════"

# Run one lane's tickets in order. $2 = optional worktree dir (empty ⇒ shepherd
# default). Returns the first non-zero shepherd exit; stops the lane there.
run_chain() {
  local csv="$1"
  local wt="${2:-}"
  local wt_args=()
  [ -n "$wt" ] && wt_args=(--worktree "$wt")
  IFS=',' read -ra tickets <<< "$csv"
  echo ""
  echo "── lane${wt:+ [$wt]}: ${tickets[*]} ──"
  local t rc
  for t in "${tickets[@]}"; do
    t="$(echo "$t" | tr -d '[:space:]')"
    [ -z "$t" ] && continue
    echo "[orchestrate] → shepherd $t ${PASSTHRU[*]:-}"
    if [ "$DRY_RUN" = "1" ]; then
      echo "[orchestrate]   (dry-run) bash $SHEPHERD --no-tmux ${wt_args[*]:-} ${PASSTHRU[*]:-} $t"
      continue
    fi
    # `${arr[@]+"${arr[@]}"}` expands to nothing on an empty array and to the
    # quoted elements otherwise — the portable idiom that survives `set -u` on
    # bash 3.2 (macOS), where a bare "${arr[@]}" on an empty array errors.
    bash "$SHEPHERD" --no-tmux ${wt_args[@]+"${wt_args[@]}"} ${PASSTHRU[@]+"${PASSTHRU[@]}"} "$t"
    rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "[orchestrate] ✗ $t exited $rc — STOPPING this lane (downstream tickets" >&2
      echo "              depend on / conflict with it; do not skip ahead)." >&2
      return "$rc"
    fi
    echo "[orchestrate] ✓ $t reached terminal state"
  done
  return 0
}

OVERALL_RC=0

if [ "$EXECUTE" = "1" ]; then
  # ── Concurrent lane executor ──────────────────────────────────────────────
  # Launch each lane in the background in its own worktree, throttled to
  # MAX_CONCURRENT live jobs. bash-3.2-safe: `jobs -rp` counts running bg jobs
  # (no `wait -n`); we reap every PID at the end and keep the first failure.
  declare -a PIDS=()
  declare -a PID_LABEL=()
  i=0
  for lane in "${LANES[@]}"; do
    # Throttle: block until a slot frees.
    while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$MAX_CONCURRENT" ]; do
      sleep 2
    done
    wt="$REPO_DIR/.worktrees/shepherd-lane-${i}"
    run_chain "$lane" "$wt" &
    PIDS+=($!)
    PID_LABEL+=("lane-${i}: ${lane}")
    i=$((i + 1))
    # Small stagger so concurrent `git worktree add` calls don't race on the
    # repo's worktree metadata lock.
    sleep 1
  done

  # Reap all lanes; record the first failure (honest aggregate exit).
  FIRST_FAIL=""
  j=0
  while [ "$j" -lt "${#PIDS[@]}" ]; do
    wait "${PIDS[$j]}"
    rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "[orchestrate] ✗ ${PID_LABEL[$j]} → rc=$rc" >&2
      if [ -z "$FIRST_FAIL" ]; then
        OVERALL_RC="$rc"
        FIRST_FAIL="${PID_LABEL[$j]}"
      fi
    else
      echo "[orchestrate] ✓ ${PID_LABEL[$j]} done"
    fi
    j=$((j + 1))
  done
else
  # ── Legacy serial path (unchanged) ────────────────────────────────────────
  for lane in "${LANES[@]}"; do
    # NOTE: do NOT write `if ! run_chain ...; then rc=$?` — `$?` there is the
    # exit status of the `!` pipeline (0 when run_chain failed), so a halted
    # lane would be reported as success. Capture run_chain's own status directly.
    run_chain "$lane"
    rc=$?
    if [ "$rc" -ne 0 ]; then
      OVERALL_RC="$rc"
      break
    fi
  done
fi

echo ""
echo "═══════════════════════════════════════"
if [ "$OVERALL_RC" -eq 0 ]; then
  echo "  orchestrate: all scheduled tickets reached Done"
else
  echo "  orchestrate: halted (rc=$OVERALL_RC) — see the failing ticket above"
fi
echo "═══════════════════════════════════════"
exit "$OVERALL_RC"
