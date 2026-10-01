#!/bin/bash
# The drivers keep the .env keys unexported too, and the runtime no longer sources .env (v3.2, S1).
#
# The queue loop, its supervisor, the tick, the shepherd and the worker read .env like a stage
# (bureau_load_env), and bureau-runtime.py read it for its own Linear calls through an inline
# bash that ran `set -a; source "$BUREAU_ENV_FILE"`: that executed the file as shell code (a
# line `NAME= command` ran the command, the risk bureau_load_env exists for) and exported every
# key to the processes that bash started. Each driver now keeps the three keys unexported, and
# the runtime reads .env through bureau_load_env.
#
# Runs the REAL scripts in the sandbox of tests/lib/pr5-interrupt.sh (curl plays Linear and
# answers only a request that carries the sandbox key, so a run that reaches Done proves the
# key still reached curl, on stdin), with the probes of tests/lib/c1-env-probes.sh in front of
# PATH; the .env carries one line that runs a command when the file is sourced.
#   1. shepherd → worker → runtime → spec probe stage, to Done: no process any of them starts
#      after reading .env carries a key; the stage's Linear calls went through (Done)
#   2. the runtime's own Linear calls (bureau-runtime.py shell(), here the ownership halt of
#      tests/test_ownership_halt_trace.sh case 1: a shepherd on a ticket an interrupted run still
#      holds ends 21 and leaves needs-human and a comment): the halt reached Linear, the .env line
#      ran nowhere, and the processes that inline bash started carry no key
#   3. bureau-tick.sh (no stage enabled): its processes carry no key, also when the shell that
#      starts it exported the keys that .env defines
#   4. queue-loop-supervised.sh → queue-loop.sh, one round: no process carries a key
# Negative control: against v3.1.0 (9411b3b) all four fail ("jq LINEAR_API_KEY …"), and 2 also
# fails on the executed .env line.
set -euo pipefail
source "$(dirname "$0")/lib/pr5-interrupt.sh"
source "$(dirname "$0")/lib/c1-env-probes.sh"

fail() { c1_fail "$@"; }
pr5_setup
trap pr5_teardown EXIT
PROBES="$SB/c1bin"
c1_probe_tools "$PROBES" "$SB/probe.log" jq python3 date mktemp cat sed grep head tail tr wc sort cut mkdir rm basename dirname tee sleep
# setup_repo — a fresh pr5 repository whose .env also carries the marker and the sourced-only line.
setup_repo() {
  pr5_new_repo
  chmod +x "$REPO/scripts/"*.sh   # as bureau_install.py installs them (queue-loop.sh is 0644 in git)
  printf '%s=%s\nC1_NOT_A_KEY= touch %s/env-executed\n' "$C1_MARK_NAME" "$C1_MARK_VALUE" "$SB" >> "$REPO/.env"
  rm -f "$SB/env-executed"; : > "$SB/probe.log"
}

# ── 1. shepherd → worker → runtime → stage ────────────────────────────────────
setup_repo
printf finish > "$SB/probe-mode"
PATH="$PROBES:$PATH" pr5_shepherd
[ "$RC" = 0 ] || fail "1: the shepherd ended $RC, wanted 0: $(printf '%s\n' "$ERR" | tail -3 | tr '\n' ' ')"
grep -qx EXP-7 "$SB/finished.log" 2>/dev/null || fail "1: the probe stage did not run to its end"
[ "$(cat "$SB/state")" = s8 ] || fail "1: the ticket did not reach Done"
if grep -qx unauthorized "$SB/linear.log"; then fail "1: a Linear request went out without the key"; fi
c1_check_log "$SB/probe.log" "1 shepherd chain"
grep -q '^python3 ' "$SB/probe.log" || fail "1: no python3 (runtime, supervision) was recorded"
[ ! -e "$SB/env-executed" ] || fail "1: a line of .env ran as a command"
[ "$C1_FAILS" = 0 ] && echo "PASS 1 shepherd, worker, runtime and stage start no process with a .env key; Linear still answered"

# ── 2. the runtime's own Linear calls ─────────────────────────────────────────
F2=$C1_FAILS
setup_repo
pr5_ticket 7 '["lane-2"]'
PATH="$PROBES:$PATH" pr5_shepherd_start || fail "2: the probe stage did not start"
pr5_interrupt
[ "$RC" = 130 ] || fail "2: the interrupted shepherd ended $RC, wanted 130"
: > "$SB/linear.log"; : > "$SB/probe.log"
PATH="$PROBES:$PATH" pr5_shepherd
[ "$RC" = 21 ] || fail "2: the shepherd on a held ticket ended $RC, wanted 21"
grep -qx 'add-label 7 needs-human' "$SB/linear.log" || fail "2: the runtime's halt did not set needs-human: $(tr '\n' ' ' < "$SB/linear.log")"
grep -qx 'comment 7' "$SB/linear.log" || fail "2: the runtime's halt did not comment"
if grep -qx unauthorized "$SB/linear.log"; then fail "2: a Linear request of the runtime went out without the key"; fi
[ ! -e "$SB/env-executed" ] || fail "2: the runtime ran a line of .env as a command (it sourced the file)"
c1_check_log "$SB/probe.log" "2 runtime halt"
[ "$C1_FAILS" = "$F2" ] && echo "PASS 2 the runtime reads .env without running it and hands no key to what its bash starts; the halt reached Linear"

# ── 3. bureau-tick.sh ─────────────────────────────────────────────────────────
F3=$C1_FAILS
setup_repo
set +e
(cd "$REPO" && PATH="$PROBES:$PATH" _pr5_env bash scripts/bureau-tick.sh --result-file "$SB/tick.json" > "$SB/out" 2> "$SB/err")
RC=$?
set -e
[ "$RC" = 0 ] || fail "3: the tick ended $RC: $(tail -3 "$SB/err" | tr '\n' ' ')"
[ "$(jq -r .outcome "$SB/tick.json" 2>/dev/null)" = waiting ] || fail "3: the tick did not report waiting"
c1_check_log "$SB/probe.log" "3 tick"
# The operator's shell exported the keys too (and .env defines them): the tick starts with them,
# but what it starts does not get them.
: > "$SB/probe.log"
set +e
(cd "$REPO" && PATH="$PROBES:$PATH" _pr5_env LINEAR_API_KEY=k TELEGRAM_BOT_TOKEN=t TELEGRAM_ALERT_CHAT_ID=c \
   bash scripts/bureau-tick.sh --result-file "$SB/tick.json" > "$SB/out" 2> "$SB/err")
RC=$?
set -e
[ "$RC" = 0 ] || fail "3 exported: the tick ended $RC: $(tail -3 "$SB/err" | tr '\n' ' ')"
c1_check_log "$SB/probe.log" "3 tick started with the keys exported"
[ "$C1_FAILS" = "$F3" ] && echo "PASS 3 the tick starts no process with a .env key"

# ── 4. queue-loop-supervised.sh → queue-loop.sh ───────────────────────────────
F4=$C1_FAILS
setup_repo
set -m
(cd "$REPO" && exec env PATH="$PROBES:$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_DISABLE_THROTTLE=1 \
   bash "$REPO/scripts/queue-loop-supervised.sh" all 1 > "$SB/out" 2> "$SB/err") &
LOOP=$!
set +m
# One round ends with the loop's `sleep` until the next check.
for _ in $(seq 1 300); do grep -q '^sleep ' "$SB/probe.log" 2>/dev/null && break; sleep 0.1; done
kill -KILL -- "-$LOOP" 2>/dev/null || true
wait "$LOOP" 2>/dev/null || true
grep -q '^sleep ' "$SB/probe.log" || fail "4: the queue loop did not finish a round: $(tail -3 "$SB/err" | tr '\n' ' ')"
grep -qE 'Queues drained|All queues empty' "$REPO/logs/queue-all.log" 2>/dev/null || fail "4: the queue loop logged no round"
grep -q 'Supervisor starting' "$REPO/logs/supervisor-all.log" 2>/dev/null || fail "4: the supervisor did not start"
c1_check_log "$SB/probe.log" "4 queue loop and supervisor"
[ "$C1_FAILS" = "$F4" ] && echo "PASS 4 the queue loop and its supervisor start no process with a .env key"

if [ "$C1_FAILS" != 0 ]; then echo "$C1_FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_env_keys_drivers"
