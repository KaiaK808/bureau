#!/bin/bash
# test_session_throttle.sh — EXP-670 session-usage throttle: pure decision,
# lenient signal parser, and the guard's no-op / fast-proceed / disable paths.
# (Ported from brainhuggers-cli.)

set -uo pipefail
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BC="$TESTS_DIR/../templates/scripts/bureau-config.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# bureau-config.sh's _find_config exits if no .bureau.json in cwd — give it one.
cd "$TMP"
echo '{"session":{"usage_threshold_pct":80,"pause_on_stale_data":false}}' > .bureau.json
# shellcheck disable=SC1090
source "$BC" >/dev/null 2>&1
set +e

now=$(date +%s)
fail=0
check() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1 (got: ${3:-})"; fail=1; fi; }

# pure decision
check "below threshold → proceed" '[ "$(_throttle_decide 50 0 0 80 false "$now")" = "proceed" ]' "$(_throttle_decide 50 0 0 80 false "$now")"
check "over, reset in 100s → pause 105" '[ "$(_throttle_decide 90 "$((now+100))" "$now" 80 false "$now")" = "pause 105" ]' "$(_throttle_decide 90 "$((now+100))" "$now" 80 false "$now")"
check "over, no reset → pause 300" '[ "$(_throttle_decide 90 0 "$now" 80 false "$now")" = "pause 300" ]'
check "float pct 82.5 over 80 → pauses" '[ "$(_throttle_decide 82.5 0 "$now" 80 false "$now")" = "pause 300" ]'
check "stale → proceed (no stale_pause)" '[ "$(_throttle_decide 90 0 "$((now-600))" 80 false "$now")" = "proceed" ]'
check "stale + pause_on_stale_data → pauses" '[ "$(_throttle_decide 90 0 "$((now-600))" 80 true "$now")" = "pause 300" ]'
check "reset far future → capped 3600" '[ "$(_throttle_decide 90 "$((now+99999))" "$now" 80 false "$now")" = "pause 3600" ]'

# signal parsing
echo '{"pct":73,"reset_epoch":123,"updated_epoch":456}' > "$TMP/u.json"
sig=$(BRAINHUGGERS_USAGE_FILE="$TMP/u.json" _session_usage_signal)
check "our format parses pct|reset|upd" '[ "$sig" = "73|123|456" ]' "$sig"
echo '{"usage_pct":91,"resets_at_epoch":789,"timestamp":111}' > "$TMP/cw.json"
sig2=$(BRAINHUGGERS_USAGE_FILE="$TMP/cw.json" _session_usage_signal)
check "lenient aliases parse" '[ "$sig2" = "91|789|111" ]' "$sig2"

# guard paths
BRAINHUGGERS_USAGE_FILE="$TMP/none.json" session_throttle_guard >/dev/null 2>&1
check "no signal → no-op rc 0" '[ "$?" -eq 0 ]'
echo "{\"pct\":10,\"reset_epoch\":0,\"updated_epoch\":$now}" > "$TMP/low.json"
t0=$(date +%s); BRAINHUGGERS_USAGE_FILE="$TMP/low.json" session_throttle_guard >/dev/null 2>&1; t1=$(date +%s)
check "under threshold → returns fast" '[ "$((t1-t0))" -le 2 ]' "$((t1-t0))s"
echo "{\"pct\":99,\"reset_epoch\":0,\"updated_epoch\":$now}" > "$TMP/hi.json"
t0=$(date +%s); BUREAU_DISABLE_THROTTLE=1 BRAINHUGGERS_USAGE_FILE="$TMP/hi.json" session_throttle_guard >/dev/null 2>&1; t1=$(date +%s)
check "BUREAU_DISABLE_THROTTLE → short-circuits" '[ "$((t1-t0))" -le 2 ]' "$((t1-t0))s"

[ "$fail" -eq 0 ] && echo "PASS — session throttle"
exit "$fail"
