#!/bin/bash
# No stage outlives an interrupt, and a stage's own cleanup gets its time (v3.1.0-rc.2).
#
# The runtime wrappers of one run nest (shepherd → worker → stage). Each forwards SIGTERM to its
# child's process group and SIGKILLs that group when the child has not ended after a grace
# period. Once the worker waited for its stage on a signal (it ends with 130 now), the worker's
# runtime killed the worker's group, the stage's runtime with it, at the same moment the stage's
# runtime was due to kill the stage: a stage that ignored the signal ran on (5 of 7 runs in the
# verifier's probe), and the worker's cleanup was cut. Each level now waits one step longer than
# the level inside it (bureau-runtime.py stop_grace: 20, 15, 10, 5 s; BUREAU_STOP_GRACE_SECONDS
# sets the step).
#
# Runs the REAL shepherd.sh → bureau-worker.sh → probe stage chain (tests/lib/pr5-interrupt.sh).
#   1. a stage that ignores SIGTERM and Ctrl-C, interrupted ten times (step 1 s): each time the
#      whole chain ends by itself, nothing survives, exit 130
#   2. a stage whose EXIT trap needs 7 s (like implement's deferred push), default step: the trap
#      finishes, the worker's cleanup runs to its end, nothing survives
# Negative control: against v3.1.0-rc.1 (5184cf8) the stage's runtime kills the stage after 5 s
# and case 2 fails (no push); at the first rc.2 head (a75f9e8) case 1 leaves the stage running.
set -euo pipefail
source "$(dirname "$0")/lib/pr5-interrupt.sh"

fail() {
  echo "FAIL $*" >&2
  printf '  | rc=%s\n' "${RC:-}" >&2
  printf '%s\n' "${ERR:-}" | tail -20 | sed 's/^/  | err: /' >&2
  exit 1
}
pr5_setup
trap pr5_teardown EXIT

# gone <seconds> — wait until no process of the sandbox repository is left; fails when one is.
gone() {
  local i=0 limit=$(( $1 * 10 ))
  while ps -A -o args= | grep -F "$REPO/" | grep -v grep >/dev/null; do
    [ "$i" -lt "$limit" ] || return 1
    sleep 0.1; i=$((i + 1))
  done
}

# ── 1. a stage that ignores the signal ─────────────────────────────────────────────────
export BUREAU_STOP_GRACE_SECONDS=1
for n in 1 2 3 4 5 6 7 8 9 10; do
  pr5_new_repo
  printf ignore > "$SB/probe-mode"
  pr5_shepherd_start || fail "1.$n: the probe stage did not start"
  pr5_interrupt
  [ "$RC" = 130 ] || fail "1.$n: the interrupted shepherd ended $RC, wanted 130"
  gone 15 || fail "1.$n: a process of the run outlived the interrupt: $(ps -A -o pid=,pgid=,args= | grep -F "$REPO/" | grep -v grep | sed "s#$SB#<sandbox>#g")"
done
unset BUREAU_STOP_GRACE_SECONDS
echo "PASS 1 a stage that ignores SIGTERM: ten interrupts, the chain ends by itself every time"

# ── 2. a stage whose EXIT trap needs 7 s ───────────────────────────────────────────────
pr5_new_repo
printf slowexit > "$SB/probe-mode"
pr5_shepherd_start || fail "2: the probe stage did not start"
pr5_interrupt
[ "$RC" = 130 ] || fail "2: the interrupted shepherd ended $RC, wanted 130"
gone 30 || fail "2: a process of the run outlived the interrupt"
ERR=$(cat "$SB/err")
[ -f "$SB/pushed" ] || fail "2: the stage was killed before its 7-second EXIT trap finished"
grep -qF "Preserved unfinished work in $WT" <<< "$ERR" || fail "2: the worker's cleanup did not run to its end"
[ "$(grep -c '^Interrupted Bureau run ' <<< "$ERR")" = 1 ] || fail "2: the interrupt message is not printed exactly once"
echo "PASS 2 a stage whose EXIT trap needs 7 s: it finishes, and so does the worker's cleanup"
