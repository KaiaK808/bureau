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
pr2_head_age 3600;       grace_case 'old head, default grace'     25 blocked "^ci_green: ci: no check run and no status on .* after its commit (agents.merge_ci_start_grace_seconds: 1800)"
pr2_head_age 60;         grace_case 'fresh head'                   2 not-yet '^ci_green: ci: only 0 completed check(s)'
pr2_head_age 1799;       grace_case 'one second inside the grace'  2 not-yet 'only 0 completed'
pr2_head_age 1800;       grace_case 'at the grace'                25 blocked 'no check run and no status'
pr2_head_age unreadable; grace_case 'head time unreadable'         2 not-yet 'only 0 completed'
pr2_head_age -600;       grace_case 'head time in the future'      2 not-yet 'only 0 completed'
pr2_head_age 3600
pr2_config '.agents.merge_ci_start_grace_seconds = 7200'; grace_case 'configured 7200'   2 not-yet 'only 0 completed'
pr2_config '.agents.merge_ci_start_grace_seconds = 0';    grace_case 'configured 0'     25 blocked 'grace_seconds: 0)'
for bad in '"600"' '-5' '1.5' '1e20' 'true'; do
  pr2_config ".agents.merge_ci_start_grace_seconds = $bad"; grace_case "invalid $bad → 1800" 25 blocked 'grace_seconds: 1800)'
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
echo 'PASS 2 no check and no status past the grace is blocked; before it, and on every failed read, not yet'

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
echo 'PASS negative controls: v3.0.2 keeps the CI gate under false, and waits forever without checks'

echo 'OK test_merge_gate_switches'
