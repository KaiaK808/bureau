#!/bin/bash
# The merge gate's two switches and its CI start grace.
#
#   1. agents.merge_require_green_ci / merge_require_up_to_date: false switches the gate
#      off. The gate read them with `jq '.x // true'`, which turns false into true, so the
#      documented opt-out never worked. Absent, null and true keep the gate; any other
#      value ("false" as a string, 0, "no") keeps it and warns.
#   2. A head that carries no check run and no status at all is blocked once its commit
#      is older than agents.merge_ci_start_grace_seconds (default 1800); before that, and
#      whenever a read fails, it stays "not yet". It used to stay "not yet" forever, so a
#      repository without a workflow waited silently.
#      The gate line carries no age, so polling a blocked head posts one PR comment.
#   3. agents.merge_min_required_checks and the grace follow one rule (_merge_gate_number,
#      the same as the doctor's): a string of digits is that number, a fraction is rounded
#      up, a negative number or anything else is the default, each with a warning. Before,
#      a string made the count test fail and the check pass with no check at all, and -1
#      meant no check needed; "2" must never count as less than 2.
#
# Runs the REAL merge-pipeline.sh (on its own, with a gate report) and the real gate
# helpers from bureau-config.sh against the pr2 gh double. Negative controls put the
# v3.0.2 reads back into the sandbox copy.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP
ISSUE=EXP-802
trap 'teardown || true' EXIT
fail() { echo "FAIL: $1" >&2; printf '%s\n--- stderr ---\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }

new_sandbox() {
  teardown || true
  sandbox_init "$ISSUE" test-branch
  export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  pr2_gate_setup
  # The review's APPROVE the gate reads.
  jq -n '[{createdAt: "2026-09-29T09:00:00Z", body: "## Code Review v2 — EXP-802\n\n**Verdict**: APPROVE"}]' > "$PR2_GH/comments.json"
}
gate() {  # runs the merge stage; sets LAST_RC, OUTCOME and LINES
  export BUREAU_MERGE_GATE_REPORT="$SANDBOX/gate.report"
  rm -f "$BUREAU_MERGE_GATE_REPORT" "$PR2_GH/merge_calls.log"
  jq '.state = "OPEN"' "$PR2_GH/pr.json" > "$PR2_GH/pr.tmp" && mv "$PR2_GH/pr.tmp" "$PR2_GH/pr.json"
  run_pipeline merge-pipeline.sh "$ISSUE"
  unset BUREAU_MERGE_GATE_REPORT
  OUTCOME=$(head -n 1 "$SANDBOX/gate.report" 2>/dev/null || true)
  LINES=$(sed -n '2,$p' "$SANDBOX/gate.report" 2>/dev/null || true)
}
stale_base() { jq -n '{commit: {sha: "0000000000000000000000000000000000000001"}}' > "$PR2_GH/branch.json"; echo '{"ahead_by":2}' > "$PR2_GH/compare.json"; }

# ── 1a. merge_require_green_ci ─────────────────────────────────────────────
new_sandbox
pr2_checks red
pr2_config '.agents.merge_require_green_ci = false'
gate
[ "$LAST_RC" = 0 ] && pr2_merged || fail "1a: false did not switch the CI gate off (rc $LAST_RC, report: $LINES)"
grep -q 'must be true or false' <<< "$LAST_STDERR" && fail '1a: warned about a valid false'
for value in absent null true '"false"' 0 '"no"'; do
  if [ "$value" = absent ]; then pr2_config 'del(.agents.merge_require_green_ci)'
  else pr2_config ".agents.merge_require_green_ci = $value"; fi
  gate
  [ "$LAST_RC" = 25 ] && ! pr2_merged && grep -q '^ci_green: ci: failing check' <<< "$LINES" \
    || fail "1a: $value did not keep the CI gate (rc $LAST_RC, report: $LINES)"
  case "$value" in
    absent|null|true) grep -q 'merge_require_green_ci must be true or false' <<< "$LAST_STDERR" && fail "1a: warned about $value" ;;
    *) grep -q 'merge_require_green_ci must be true or false' <<< "$LAST_STDERR" || fail "1a: no warning for $value" ;;
  esac
done
echo 'PASS 1a merge_require_green_ci: false switches it off; absent, null, true and every other value keep it'

# ── 1b. merge_require_up_to_date ───────────────────────────────────────────
new_sandbox
stale_base
pr2_config '.agents.merge_require_up_to_date = false'
gate
[ "$LAST_RC" = 0 ] && pr2_merged || fail "1b: false did not switch the base gate off (rc $LAST_RC, report: $LINES)"
for value in absent '"false"'; do
  if [ "$value" = absent ]; then pr2_config 'del(.agents.merge_require_up_to_date)'
  else pr2_config ".agents.merge_require_up_to_date = $value"; fi
  gate
  [ "$LAST_RC" = 25 ] && grep -q '^base_current: base: PR #99 is 2 commit(s) behind main' <<< "$LINES" \
    || fail "1b: $value did not keep the base gate (rc $LAST_RC, report: $LINES)"
done
echo 'PASS 1b merge_require_up_to_date: false switches it off; absent and the string "false" keep it'

# ── 2. the CI start grace ──────────────────────────────────────────────────
new_sandbox
pr2_checks none
grace_case() {  # <label> <want rc> <want outcome> <pattern>
  gate
  [ "$LAST_RC" = "$2" ] && [ "$OUTCOME" = "$3" ] && grep -q -- "$4" <<< "$LINES" \
    || fail "2 $1: rc $LAST_RC outcome '$OUTCOME', wanted $2 $3 /$4/ in: $LINES"
  pr2_merged && fail "2 $1: merged"
  return 0
}
pr2_head_age 3600;       grace_case 'old head, default grace'     25 blocked "^ci_green: ci: no check run and no status on [0-9a-f]*, and its commit is older than the CI start grace (agents.merge_ci_start_grace_seconds: 1800) — no CI started for this head\$"
pr2_head_age 60;         grace_case 'fresh head'                   2 not-yet '^ci_green: ci: only 0 completed check(s)'
pr2_head_age 1799;       grace_case 'one second inside the grace'  2 not-yet 'only 0 completed'
pr2_head_age 1800;       grace_case 'at the grace'                25 blocked 'no check run and no status'
pr2_head_age unreadable; grace_case 'head time unreadable'         2 not-yet 'only 0 completed'
pr2_head_age empty;      grace_case 'head time empty'              2 not-yet 'only 0 completed'
pr2_head_age -600;       grace_case 'head time in the future'      2 not-yet 'only 0 completed'
pr2_head_age 3600
pr2_config '.agents.merge_ci_start_grace_seconds = 7200'; grace_case 'configured 7200'   2 not-yet 'only 0 completed'
pr2_config '.agents.merge_ci_start_grace_seconds = 0';    grace_case 'configured 0'     25 blocked 'grace_seconds: 0)'
grep -q 'should be a whole number' <<< "$LAST_STDERR" && fail '2: warned about a valid grace'
# value : grace used : outcome for a head 3600 s old
for row in '"600":600:blocked' '1.5:2:blocked' '-5:1800:blocked' 'true:1800:blocked' '"abc":1800:blocked' \
           '1e20:9999999:not-yet' '12345678901234567890:9999999:not-yet'; do
  value=${row%%:*}; rest=${row#*:}; used=${rest%%:*}; want=${rest#*:}
  pr2_config ".agents.merge_ci_start_grace_seconds = $value"
  if [ "$want" = blocked ]; then grace_case "grace $value → $used" 25 blocked "grace_seconds: $used)"
  else grace_case "grace $value → $used" 2 not-yet 'only 0 completed'; fi
  grep -q "agents.merge_ci_start_grace_seconds should be a whole number of at least 0; using $used\$" <<< "$LAST_STDERR" \
    || fail "2: no warning naming $used for the grace $value: $LAST_STDERR"
done
pr2_config 'del(.agents.merge_ci_start_grace_seconds)'
touch "$PR2_GH/fail_status"; grace_case 'status read failed' 2 not-yet 'only 0 completed'; rm -f "$PR2_GH/fail_status"
echo '{"statuses":[{"context":"ext","state":"pending"}]}' > "$PR2_GH/status.json"
grace_case 'a pending status' 2 not-yet 'still pending'
echo '{"statuses":[]}' > "$PR2_GH/status.json"
pr2_checks pending; grace_case 'a queued check run' 2 not-yet 'still pending'
# Fewer completed checks than required, but not none: the grace is only for a bare head.
pr2_checks green; pr2_config '.agents.merge_min_required_checks = 2'
grace_case 'one of two required checks' 2 not-yet 'only 1 completed check(s)'
pr2_config 'del(.agents.merge_min_required_checks)'
pr2_checks none
# merge_min_required_checks 0 says zero checks are fine: no blocker at all.
pr2_config '.agents.merge_min_required_checks = 0'
gate
[ "$LAST_RC" = 0 ] && pr2_merged || fail "2: merge_min_required_checks 0 no longer merges a head without checks (rc $LAST_RC: $LINES)"
# Polled while it stays blocked (the merge stage sets no needs-human on 25, so the queue
# takes the ticket again every poll): one gate comment on the PR, not one per poll.
new_sandbox
pr2_checks none
for age in 3600 3660 3720 3780; do pr2_head_age "$age"; gate; done
[ "$LAST_RC" = 25 ] || fail "2: the polled head ended $LAST_RC, wanted 25"
comments=$(jq '[.[] | select(.body | test("Bureau merge gate"))] | length' "$PR2_GH/comments.json")
[ "$comments" = 1 ] || fail "2: four polls of the same blocked head posted $comments gate comments, wanted 1"
echo 'PASS 2 no check and no status past the grace is blocked (one PR comment however often polled); before it, and on every failed read, not yet'

# ── 3. merge_min_required_checks by the shared rule ────────────────────────
# One green check on the head. value : checks required : merges?
new_sandbox
pr2_head_age 60
for row in '"2":2:no' '"3":3:no' '1.5:2:no' '"abc":1:yes' '-1:1:yes' '"-1":1:yes' 'true:1:yes' '[]:1:yes' \
           '12345678901234567890:9999999:no'; do
  value=${row%%:*}; rest=${row#*:}; need=${rest%%:*}; merges=${rest#*:}
  pr2_checks green; pr2_config ".agents.merge_min_required_checks = $value"
  gate
  if [ "$merges" = yes ]; then
    [ "$LAST_RC" = 0 ] && pr2_merged || fail "3: $value (= $need) with one green check did not merge (rc $LAST_RC: $LINES)"
  else
    [ "$LAST_RC" = 2 ] && ! pr2_merged && grep -q "^ci_green: ci: only 1 completed check(s) on .* (require >= $need)\$" <<< "$LINES" \
      || fail "3: $value did not require $need checks (rc $LAST_RC, report: $LINES)"
  fi
  grep -q "agents.merge_min_required_checks should be a whole number of at least 0; using $need\$" <<< "$LAST_STDERR" \
    || fail "3: no warning naming $need for merge_min_required_checks $value: $LAST_STDERR"
done
# With no check at all, a value that falls back to 1 still waits.
pr2_checks none; pr2_config '.agents.merge_min_required_checks = "abc"'; gate
[ "$LAST_RC" = 2 ] && grep -q '(require >= 1)$' <<< "$LINES" || fail "3: \"abc\" with no check did not wait for 1 (rc $LAST_RC: $LINES)"
# Valid values: 3 is 3, 2.0 is 2, absent is 1, all without a warning.
for row in '3:3' '2.0:2' 'absent:1'; do
  value=${row%%:*}; need=${row#*:}
  if [ "$value" = absent ]; then pr2_config 'del(.agents.merge_min_required_checks)'; else pr2_config ".agents.merge_min_required_checks = $value"; fi
  gate
  grep -q "(require >= $need)\$" <<< "$LINES" || fail "3: $value was not read as $need: $LINES"
  grep -q 'should be a whole number' <<< "$LAST_STDERR" && fail "3: warned about the valid value $value"
done
echo 'PASS 3 merge_min_required_checks: "2" requires 2, a fraction is rounded up, a negative or unreadable value is 1, each with a warning'

# ── Negative controls: the v3.0.2 reads ────────────────────────────────────
new_sandbox
python3 - "$SCRIPTS_DIR/merge-pipeline.sh" <<'PY'
import sys
p = sys.argv[1]; src = open(p).read()
for key in ('merge_require_green_ci', 'merge_require_up_to_date'):
    old = '$(merge_gate_required %s)' % key
    assert src.count(old) == 1, old
    src = src.replace(old, "$(bureau_get '.agents.%s // true')" % key)
open(p, 'w').write(src)
PY
pr2_checks red
pr2_config '.agents.merge_require_green_ci = false'
gate
[ "$LAST_RC" = 25 ] && grep -q '^ci_green:' <<< "$LINES" || fail "negative control: the v3.0.2 read should keep the CI gate under false (rc $LAST_RC)"
new_sandbox
python3 - "$SCRIPTS_DIR/real-helpers.sh" <<'PY'
import sys
p = sys.argv[1]; src = open(p).read()
old = '    if [ "$total_completed" = 0 ] && [ "$statuses_read" = ok ]; then\n'
assert src.count(old) == 1, 'grace branch not found'
open(p, 'w').write(src.replace(old, '    if false; then\n'))
PY
pr2_checks none; pr2_head_age 86400
gate
[ "$LAST_RC" = 2 ] && [ "$OUTCOME" = not-yet ] || fail "negative control: without the grace a day-old head without checks should stay not yet (rc $LAST_RC)"
new_sandbox
python3 - "$SCRIPTS_DIR/real-helpers.sh" <<'PY'
import sys
p = sys.argv[1]; src = open(p).read()
old = '  min_required=$(_merge_gate_number merge_min_required_checks 1) || true\n'
assert src.count(old) == 1, 'min_required read not found'
open(p, 'w').write(src.replace(old, "  min_required=$(bureau_get '.agents.merge_min_required_checks // 1')\n"))
PY
pr2_checks none; pr2_head_age 60
pr2_config '.agents.merge_min_required_checks = "abc"'
gate
[ "$LAST_RC" = 0 ] && pr2_merged || fail "negative control: the v3.0.2 read should merge a head without checks under \"abc\" (rc $LAST_RC)"
echo 'PASS negative controls: v3.0.2 keeps the CI gate under false, waits forever without checks, and merges without checks under "abc"'

echo 'OK test_merge_gate_switches'
