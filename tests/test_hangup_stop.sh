#!/bin/bash
# A hang-up stops a run like SIGTERM (v3.2.0, O1).
#
# Closing the terminal, the ssh connection or the tmux session sends SIGHUP to the one wrapper
# of the run on that terminal: the runtime that shepherd.sh (or a queue worker) re-executes
# into. Each wrapper starts its child in a session of its own, so nothing else of the run
# hears the hang-up. Until v3.1 the runtime trapped only SIGTERM and Ctrl-C: a hang-up ended
# that outer wrapper alone, and the shepherd, the worker and the stage ran on detached, with
# the claim, until someone sent SIGTERM to their process groups by hand (the v3.1 pilot). Now
# the runtime and the provider adapter stop on SIGHUP as on SIGTERM (their child gets
# SIGTERM), the shepherd, the worker and the queue supervisor trap it like SIGTERM, and the
# shepherd's release and the runtime's exit code survive a terminal that is gone.
#
# Runs the REAL shepherd.sh → bureau-worker.sh → probe stage chain (tests/lib/pr5-interrupt.sh)
# with the real runtime and config, on a pseudo-terminal of its own (tests/lib/hangup.py);
# Linear and Telegram are the curl double.
#   1. the terminal closes (output to files): exit 130, the resume steps once, the claim
#      released, the work and the interrupted leases kept for the resume, nothing else
#      written, nothing left running
#   2. the terminal closes with the output on it, as in a tmux pane, so every later write
#      fails: still 130 (not 120), the claim released, nothing left running
#  2b. the same while the shepherd itself waits (60 s before a retry): released with one
#      attempt, as a cancelled run
#   3. SIGHUP to the run's process group (what an interactive shell sends its jobs when its
#      terminal goes): the same as 1
#   4. a stage that ignores SIGHUP stops at once: it gets SIGTERM, and its EXIT trap runs
#   5. a stage that ignores SIGHUP, SIGTERM and Ctrl-C (step 1 s), three runs: the chain ends by
#      itself every time, nothing left running
#   6. a run started under nohup outlives its terminal, as in v3.1; SIGTERM still stops it
#   7. SIGHUP to the shepherd's own process group: cancelled like SIGTERM, exit 130, released;
#      the runtime in front got no signal and keeps the leases for the resume all the same
#  7b. SIGTERM to the shepherd's own process group while the stage needs 7 s to stop (an EXIT
#      trap such as implement's deferred push): the shepherd ends first, and the runtime in
#      front still keeps the leases interrupted and prints the steps once (the maintainer's
#      decision for v3.2: a child's 130 counts as interrupted)
#   8. SIGHUP to the worker's process group: the worker ends with 130, its work preserved; the
#      shepherd takes the stage's 130 for a halt and alerts, and the leases are kept
#   9. tmux kill-session on the shepherd's session (skipped where tmux is not installed)
#  10. SIGHUP to the queue supervisor: it stops its queue loop instead of leaving it running
# Negative control: against v3.1.0 (9411b3b) every case but 6 fails (the outer runtime dies
# with 129 and the rest of the run keeps running; the shepherd and the worker die with the
# hang-up and end with 255; the supervisor dies with 129 and leaves its queue loop behind);
# case 6 holds there as well, since v3.1 never stopped on a hang-up. Against the first head of
# this change (af97f45, before a child's 130 counted) case 7b fails on macOS's bash 3.2: the
# leases were released while the stage still ran, and no steps were printed. Case 7 holds
# there: a stage that stops at once lets the worker's runtime record the interrupt before the
# shepherd ends, the order that happened to work.
set -euo pipefail
source "$(dirname "$0")/lib/pr5-interrupt.sh"
HANGUP="$PR5_ROOT/tests/lib/hangup.py"

fail() {
  echo "FAIL $*" >&2
  printf '  | rc=%s seconds=%s\n' "${RC:-}" "${SECS:-}" >&2
  printf '%s\n' "${ERR:-}" | tail -20 | sed 's/^/  | err: /' >&2
  sed 's/^/  | linear: /' "$SB/linear.log" >&2 2>/dev/null || true
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
left() { ps -A -o pid=,pgid=,args= | grep -F "$REPO/" | grep -v grep | sed "s#$SB#<sandbox>#g" || true; }

# hup_run <how> [hangup.py options…] — the shepherd on the fixture ticket on a terminal of its own; the
# terminal hangs up (<how>: close or group) once the probe stage runs. RC and SECS (from the
# hang-up to the end); OUT and ERR when --out/--err sent them to $SB/out and $SB/err.
hup_run() {
  local how="$1" result; shift
  rm -f "$SB/probe-started" "$SB/out" "$SB/err" "$SB/tty"
  set +e
  result=$(cd "$REPO" && python3 "$HANGUP" --ready "$SB/probe-started" --how "$how" --transcript "$SB/tty" "$@" -- \
    env PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_SHEPHERD_CONFIRM_SECONDS=0 \
      BUREAU_DISABLE_THROTTLE=1 bash scripts/shepherd.sh --no-tmux --worktree .worktrees/shepherd-EXP-7 EXP-7)
  set -e
  RC=$(sed -n 's/^rc=\([0-9]*\) .*/\1/p' <<< "$result")
  SECS=$(sed -n 's/^rc=[0-9]* seconds=\(-*[0-9]*\).*/\1/p' <<< "$result")
  OUT=$(cat "$SB/out" 2>/dev/null || true); ERR=$(cat "$SB/err" 2>/dev/null || true)
}

# hup_start — the shepherd on the fixture ticket in the background (SHEP_PID, its outer runtime) with
# SIGHUP at its default, output to $SB/out and $SB/err; returns once the probe stage runs.
hup_start() {
  rm -f "$SB/probe-started"
  (cd "$REPO" && exec python3 -c 'import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_DFL); os.execvp(sys.argv[1], sys.argv[1:])' \
     env PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_SHEPHERD_CONFIRM_SECONDS=0 \
     BUREAU_DISABLE_THROTTLE=1 bash scripts/shepherd.sh --no-tmux --worktree .worktrees/shepherd-EXP-7 EXP-7 > "$SB/out" 2> "$SB/err") &
  SHEP_PID=$!
  local waited=0
  while [ ! -f "$SB/probe-started" ] && [ "$waited" -lt 300 ]; do sleep 0.1; waited=$((waited + 1)); done
  [ -f "$SB/probe-started" ]
}
hup_wait() {
  set +e
  wait "$SHEP_PID"; RC=$?
  set -e
  OUT=$(cat "$SB/out"); ERR=$(cat "$SB/err")
}

# group_of <fixed text> — the process group of the run's process that leads its own group and
# whose command line holds <fixed text> (the inner shepherd, the worker). Taken from the
# probe stage's own ancestry ($SB/run-procs, tests/lib/pr5-interrupt.sh), never from all
# processes of the host: a signal from this test can only reach this run.
group_of() {
  awk -v want="$1" '$1 == $2 && index($0, want) && !found { print $1; found = 1 }' "$SB/run-procs"
}

# stopped <case> — the run ended as a cancelled one and left everything for the resume:
# nothing of it still runs, both leases are kept and marked interrupted, the stage's work is
# in the worktree, and Linear got nothing but the claim and its release, made with a single
# attempt as on every cancelled run; no alert.
stopped() {
  gone 30 || fail "$1: a process of the run outlived the hang-up: $(left)"
  [ "$(jq '[.[] | select(.interrupted == true)] | length' "$COMMON/bureau/leases.json")" = 2 ] \
    || fail "$1: the leases are not kept as interrupted: $(cat "$COMMON/bureau/leases.json")"
  [ -f "$WT/probe-work.txt" ] || fail "$1: the stage's work is gone from the worktree"
  [ "$(pr5_writes 7)" = "$(printf 'add-label 7 shepherd-focused\nremove-label 7 shepherd-focused')" ] \
    || fail "$1: Linear got other writes than the claim and its release: $(pr5_writes 7 | tr '\n' ';')"
  [ "$(cat "$SB/release-single.log" 2>/dev/null)" = 1 ] || fail "$1: the claim was not released with a single attempt"
  [ ! -s "$SB/alerts.log" ] || fail "$1: an alert went out: $(cat "$SB/alerts.log")"
}

# steps <case> — the resume steps, printed once by the run's owner.
steps() {
  [ "$(grep -c '^Interrupted Bureau run ' <<< "$ERR")" = 1 ] || fail "$1: the interrupt message is not printed exactly once"
  grep -qF 'To resume, from ' <<< "$ERR" || fail "$1: the resume steps are missing"
  grep -qF "python3 scripts/bureau-runtime.py release $(jq -r '[.[] | .run_id][0]' "$COMMON/bureau/leases.json")" <<< "$ERR" \
    || fail "$1: the steps do not name the release of the run"
}

# ── 1. the terminal closes, output to files ───────────────────────────────────────────
pr5_new_repo
hup_run close --out "$SB/out" --err "$SB/err"
[ "$RC" = 130 ] || fail "1: a run whose terminal closed ended $RC, wanted 130"
stopped 1
steps 1
grep -qF '[shepherd] interrupted by SIGTERM — cancelled; nothing written but the release of EXP-7' <<< "$ERR" \
  || fail "1: the shepherd did not end as a cancelled run"
echo "PASS 1 the terminal closes: 130, the resume steps once, the claim released, nothing left running"

# ── 2. the terminal closes with the output on it ─────────────────────────────────────
pr5_new_repo
hup_run close
[ "$RC" = 130 ] || fail "2: a run whose terminal closed with the output on it ended $RC, wanted 130"
stopped 2
echo "PASS 2 the output was on the closed terminal: 130, the claim released, nothing left running"

# ── 2b. the same while the shepherd itself waits ────────────────────────────────────
# A stage that ends with 10 makes the shepherd wait 60 s and retry; the hang-up comes during
# that wait (a wrapper in front of sleep marks it), outside the stage call, so the shepherd's
# cancel path runs under set -e. Its lines must not end it before the single-attempt release.
pr5_new_repo
printf retry > "$SB/probe-mode"
printf '#!/bin/bash\n[ "${1:-}" != 60 ] || : > "%s/in-wait"\nexec %s "$@"\n' "$SB" "$(command -v sleep)" > "$SB/bin/sleep"
chmod +x "$SB/bin/sleep"; rm -f "$SB/in-wait"
hup_run close --ready "$SB/in-wait"
rm -f "$SB/bin/sleep"
[ "$RC" = 130 ] || fail "2b: a run whose terminal closed during the shepherd's wait ended $RC, wanted 130"
stopped 2b
echo "PASS 2b the terminal closes while the shepherd waits: 130, released with one attempt, nothing left running"

# ── 3. SIGHUP to the run's process group ─────────────────────────────────────────────
pr5_new_repo
hup_run group --out "$SB/out" --err "$SB/err"
[ "$RC" = 130 ] || fail "3: SIGHUP to the run's process group ended it with $RC, wanted 130"
stopped 3
steps 3
echo "PASS 3 SIGHUP to the run's process group: the same as a closed terminal"

# ── 4. a stage that ignores SIGHUP ───────────────────────────────────────────────────
# A step of 20 s gives the stage 40 s before its runtime would kill it: the stage ends at once
# only because SIGTERM reaches it, and the EXIT trap that writes $SB/stopped shows it was not
# killed outright.
export BUREAU_STOP_GRACE_SECONDS=20
pr5_new_repo
printf ignorehup > "$SB/probe-mode"
hup_run close --out "$SB/out" --err "$SB/err" --wait 30
unset BUREAU_STOP_GRACE_SECONDS
[ "$RC" = 130 ] || fail "4: a stage that ignores SIGHUP: the run ended $RC, wanted 130"
[ "$SECS" -lt 15 ] || fail "4: a stage that ignores SIGHUP: the run took ${SECS}s to end, wanted it at once"
[ -f "$SB/stopped" ] || fail "4: the stage did not end through its own EXIT trap (killed, or SIGTERM never reached it)"
stopped 4
echo "PASS 4 a stage that ignores SIGHUP ends at once: it gets SIGTERM, and its EXIT trap runs"

# ── 5. a stage that ignores SIGHUP, SIGTERM and Ctrl-C ───────────────────────────────
export BUREAU_STOP_GRACE_SECONDS=1
for n in 1 2 3; do
  pr5_new_repo
  printf ignoreall > "$SB/probe-mode"
  hup_run close --out "$SB/out" --err "$SB/err"
  [ "$RC" = 130 ] || fail "5.$n: a stage that ignores every signal: the run ended $RC, wanted 130"
  stopped "5.$n"
done
unset BUREAU_STOP_GRACE_SECONDS
echo "PASS 5 a stage that ignores SIGHUP, SIGTERM and Ctrl-C: three hang-ups, the chain ends by itself every time"

# ── 6. a run started under nohup ─────────────────────────────────────────────────────
pr5_new_repo
hup_run close --nohup --out "$SB/out" --err "$SB/err" --pid "$SB/pid" --wait 3
[ "$RC" = 98 ] || fail "6: a run started under nohup ended with its terminal ($RC); it should outlive it"
[ -n "$(left)" ] || fail "6: nothing of the nohup run is left running"
[ "$(jq '[.[] | select(.interrupted == true)] | length' "$COMMON/bureau/leases.json")" = 0 ] \
  || fail "6: the nohup run's leases are marked interrupted"
kill -TERM "$(cat "$SB/pid")"
stopped 6
ERR=$(cat "$SB/err")
steps 6
echo "PASS 6 a run started under nohup outlives its terminal, as before; SIGTERM still stops it"

# ── 7. SIGHUP to the shepherd's own process group ────────────────────────────────────
# The runtime in front gets no signal here. It keeps the leases because the shepherd ended
# with 130, whichever wrapper inside recorded the interrupt first, or none.
pr5_new_repo
hup_start || fail "7: the probe stage did not start"
SHEPHERD_GROUP=$(group_of "shepherd.sh --no-tmux --no-tmux --worktree $REPO/")
case "$SHEPHERD_GROUP" in ''|*[!0-9]*) fail "7: no process group of the inner shepherd: $(cat "$SB/run-procs")" ;; esac
kill -HUP -- "-$SHEPHERD_GROUP"
hup_wait
[ "$RC" = 130 ] || fail "7: SIGHUP to the shepherd's group ended the run with $RC, wanted 130"
grep -qF '[shepherd] interrupted by SIGHUP — cancelled; nothing written but the release of EXP-7' <<< "$ERR" \
  || fail "7: the shepherd did not end as a cancelled run on SIGHUP"
stopped 7
steps 7
echo "PASS 7 SIGHUP to the shepherd's own process group: cancelled like SIGTERM, 130, the leases kept"

# ── 7b. SIGTERM to the shepherd's group while the stage needs 7 s to stop ────────────
# The stage's EXIT trap takes 7 s (inside the 10 s its runtime grants). On bash 3.2 the
# subshell the shepherd runs the worker in dies of the signal at once, and the shepherd releases
# the claim and ends with 130 while the stage still runs; on a newer bash it may wait. Either
# way the runtime in front, which got no signal, keeps the leases interrupted and prints the
# steps once.
pr5_new_repo
printf slowexit > "$SB/probe-mode"
hup_start || fail "7b: the probe stage did not start"
SHEPHERD_GROUP=$(group_of "shepherd.sh --no-tmux --no-tmux --worktree $REPO/")
case "$SHEPHERD_GROUP" in ''|*[!0-9]*) fail "7b: no process group of the inner shepherd: $(cat "$SB/run-procs")" ;; esac
kill -TERM -- "-$SHEPHERD_GROUP"
hup_wait
[ "$RC" = 130 ] || fail "7b: SIGTERM to the shepherd's group ended the run with $RC, wanted 130"
grep -qF '[shepherd] interrupted by SIGTERM — cancelled' <<< "$ERR" || fail "7b: the shepherd did not end as a cancelled run"
stopped 7b
steps 7b
[ -f "$SB/pushed" ] || fail "7b: the stage was killed before its 7-second EXIT trap finished"
echo "PASS 7b SIGTERM to the shepherd's group, stage slow to stop: the leases kept, the steps once"

# ── 8. SIGHUP to the worker's process group ──────────────────────────────────────────
# The second way an inner signal ends a run: the shepherd got none, so it takes the stage's
# 130 for a stage that halted, with an alert (docs/troubleshooting.md, How to stop a run).
pr5_new_repo
hup_start || fail "8: the probe stage did not start"
WORKER_GROUP=$(group_of "$REPO/scripts/bureau-worker.sh EXP-7 spec-pipeline.sh")
case "$WORKER_GROUP" in ''|*[!0-9]*) fail "8: no process group of the worker: $(cat "$SB/run-procs")" ;; esac
kill -HUP -- "-$WORKER_GROUP"
hup_wait
gone 30 || fail "8: a process of the run outlived SIGHUP to the worker: $(left)"
[ "$RC" = 130 ] || fail "8: SIGHUP to the worker's group ended the run with $RC, wanted 130"
grep -qF '[shepherd] spec-pipeline.sh exit=130 (' <<< "$OUT" || fail "8: the worker did not end with 130: $(grep -F 'exit=' <<< "$OUT")"
grep -qF "Preserved unfinished work in $WT" <<< "$ERR" || fail "8: the worker did not preserve the stage's work"
[ "$(jq -r .reason "$COMMON"/bureau/preserved/*.json)" = interrupted ] \
  || fail "8: the worktree is not recorded as interrupted: $(cat "$COMMON"/bureau/preserved/*.json)"
[ -f "$WT/probe-work.txt" ] || fail "8: the stage's work is gone from the worktree"
[ "$(jq '[.[] | select(.interrupted == true)] | length' "$COMMON/bureau/leases.json")" = 2 ] \
  || fail "8: the leases are not kept as interrupted: $(cat "$COMMON/bureau/leases.json")"
steps 8
grep -qF 'cancelled-run' "$SB/alerts.log" || fail "8: no halt alert for the stage's 130: $(cat "$SB/alerts.log")"
echo "PASS 8 SIGHUP to the worker's process group: the worker ends with 130 and preserves the work, the leases kept, one halt alert"

# ── 9. tmux kill-session ─────────────────────────────────────────────────────────────
if command -v tmux >/dev/null 2>&1; then
  pr5_new_repo
  cat > "$SB/pane.sh" <<PANE
cd "$REPO" && exec env PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_SHEPHERD_CONFIRM_SECONDS=0 \
  BUREAU_DISABLE_THROTTLE=1 bash scripts/shepherd.sh --no-tmux --worktree .worktrees/shepherd-EXP-7 EXP-7
PANE
  rm -f "$SB/probe-started"
  if tmux -S "$SB/tmux.sock" -f /dev/null new-session -d -s hangup "exec /bin/bash '$SB/pane.sh'" 2> "$SB/tmux.err"; then
    waited=0
    while [ ! -f "$SB/probe-started" ] && [ "$waited" -lt 300 ]; do sleep 0.1; waited=$((waited + 1)); done
    [ -f "$SB/probe-started" ] || { tmux -S "$SB/tmux.sock" kill-server 2>/dev/null || true; fail "9: the probe stage did not start in tmux"; }
    tmux -S "$SB/tmux.sock" kill-session -t hangup
    RC=closed; ERR=""
    stopped 9
    tmux -S "$SB/tmux.sock" kill-server 2>/dev/null || true
    echo "PASS 9 tmux kill-session: the claim released, the work kept for the resume, nothing left running"
  else
    echo "SKIP 9 tmux could not start a session here: $(head -1 "$SB/tmux.err")"
  fi
else
  echo "SKIP 9 tmux is not installed"
fi

# ── 10. SIGHUP to the queue supervisor ───────────────────────────────────────────────
# The real queue loop under the real supervisor, with dispatch paused: the loop waits. The
# supervisor starts the loop as a program, so the scripts are executable, as the installer leaves them.
pr5_new_repo
chmod +x "$REPO/scripts/"*.sh
(cd "$REPO" && python3 scripts/bureau-runtime.py pause >/dev/null)
(cd "$REPO" && exec python3 -c 'import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_DFL); os.execvp(sys.argv[1], sys.argv[1:])' \
   env PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" bash scripts/queue-loop-supervised.sh spec 15 > "$SB/out" 2> "$SB/err") &
SUP_PID=$!
waited=0
until ps -A -o args= | grep -F "$REPO/scripts/queue-loop.sh spec 15" | grep -v grep >/dev/null || [ "$waited" -ge 300 ]; do
  sleep 0.1; waited=$((waited + 1))
done
[ "$waited" -lt 300 ] || fail "10: the supervisor did not start its queue loop"
kill -HUP "$SUP_PID"
set +e; wait "$SUP_PID"; RC=$?; set -e
[ "$RC" = 0 ] || fail "10: SIGHUP ended the supervisor with $RC, wanted 0, the code of a stop"
grep -qF 'Supervisor: shutting down (signal received)' "$REPO/logs/supervisor-spec.log" \
  || fail "10: the supervisor did not shut down on SIGHUP"
gone 15 || fail "10: the queue loop outlived its supervisor's SIGHUP: $(left)"
grep -qF 'crashed' "$REPO/logs/supervisor-spec.log" && fail "10: the supervisor took SIGHUP for a crash"
echo "PASS 10 SIGHUP to the queue supervisor: it stops its queue loop instead of leaving it running"
