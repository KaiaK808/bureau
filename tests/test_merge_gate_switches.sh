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
#   4. the merge is pinned to the head the gate checked (--match-head-commit): a push that
#      lands between the check and the merge call makes the merge refuse, and the stage
#      ends "not yet" (2) — never merged, never blocked; a head that cannot be read is not
#      yet too
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
  pr2_approval
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
touch "$PR2_GH/fail_status"; grace_case 'status read failed' 2 not-yet 'gh commit-status query failed for'; rm -f "$PR2_GH/fail_status"
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
for row in '"2":2:no' '"3":3:no' '" 2":2:no' '"+2":2:no' '"2.0":2:no' '1.5:2:no' '"abc":1:yes' '-1:1:yes' '"-1":1:yes' 'true:1:yes' '[]:1:yes' \
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

# ── 4. the merge is pinned to the head the gate checked ────────────────────
new_sandbox
OLD_HEAD=$(jq -r .headRefOid "$PR2_GH/pr.json")
echo 1111111111111111111111111111111111111111 > "$PR2_GH/move_head_on_merge"
gate
[ "$LAST_RC" = 2 ] && [ "$OUTCOME" = not-yet ] || fail "4: a head that moved during the merge ended rc $LAST_RC outcome '$OUTCOME', wanted 2 not-yet"
grep -q "^merge_head: the PR head moved from $OLD_HEAD to 1111111111111111111111111111111111111111" <<< "$LINES" || fail "4: no merge_head line: $LINES"
pr2_merged && fail '4: merged a head the gate had not checked'
grep -q $'add_issue_label\t'"$ISSUE"$'\tneeds-human' "$SANDBOX/calls.log" && fail '4: set needs-human for a moved head'
grep -q 'Merge attempted but' "$SANDBOX/calls.log" && fail '4: reported a failed merge for a moved head'
grep -q $'move_issue\t'"$ISSUE"$'\tstate-done' "$SANDBOX/calls.log" && fail '4: moved to Done without a merge'
grep -q -- '--match-head-commit'$'\t'"$OLD_HEAD" "$SANDBOX/gh_calls.log" || fail '4: the merge call was not pinned to the checked head'
# A push right after the just-in-time gate read the head (before the merge call): the
# merge stays pinned to the head the gate checked, so GitHub refuses it.
new_sandbox
echo "2 2222222222222222222222222222222222222222" > "$PR2_GH/move_head_after_reads"
gate
[ "$LAST_RC" = 2 ] && ! pr2_merged && grep -q '^merge_head: .* to 2222222222222222222222222222222222222222' <<< "$LINES" \
  || fail "4: a push after the gate's head read was merged or not reported (rc $LAST_RC: $LINES)"
# The rebase strategy is pinned the same way.
new_sandbox
sed -i.bak 's/^BUREAU_MERGE_STRATEGY="squash"/BUREAU_MERGE_STRATEGY="rebase"/' "$SCRIPTS_DIR/bureau-config.sh"
grep -q '^BUREAU_MERGE_STRATEGY="rebase"' "$SCRIPTS_DIR/bureau-config.sh" || fail '4: could not switch the stub to rebase'
gate
[ "$LAST_RC" = 0 ] && pr2_merged || fail "4: a pinned rebase merge did not go through (rc $LAST_RC)"
grep -q $'pr\tmerge\t99\t--rebase\t--match-head-commit\t'"$(jq -r .headRefOid "$PR2_GH/pr.json")" "$SANDBOX/gh_calls.log" || fail '4: the rebase merge was not pinned'
# A head that cannot be read cannot be pinned: not yet, no merge call — also with the CI
# gate off, where nothing else reads the head.
new_sandbox
touch "$PR2_GH/fail_head_read"
pr2_config '.agents.merge_require_green_ci = false'
gate
[ "$LAST_RC" = 2 ] && grep -q '^head_read: ' <<< "$LINES" || fail "4: an unreadable head ended rc $LAST_RC: $LINES"
grep -q $'pr\tmerge' "$SANDBOX/gh_calls.log" && fail '4: called gh pr merge without a readable head'
echo 'PASS 4 the merge is pinned: a head that moved or cannot be read is not yet, never merged'

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
old = '    if [ "$raw_check_count" = 0 ] && [ "$statuses_read" = ok ] && [ "$raw_status_count" = 0 ]; then\n'
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
# The v3.0.2 merge call (no --match-head-commit) merges a head that moved after the gate.
new_sandbox
python3 - "$SCRIPTS_DIR/merge-pipeline.sh" <<'PY'
import sys
p = sys.argv[1]; src = open(p).read()
old = ' --match-head-commit "$MERGE_HEAD"'
assert src.count(old) == 1, 'pinned merge call not found'
open(p, 'w').write(src.replace(old, ''))
PY
touch "$PR2_GH/allow_unpinned"
echo 1111111111111111111111111111111111111111 > "$PR2_GH/move_head_on_merge"
gate
[ "$LAST_RC" = 0 ] && pr2_merged || fail "negative control: the unpinned merge should merge the moved head (rc $LAST_RC)"
echo 'PASS negative controls: v3.0.2 keeps the CI gate under false, waits forever without checks, merges without checks under "abc", and merges a head that moved after the gate'

# Each binding scenario uses the real merge stage, real local commits and a
# file-only origin. Repeat the identical assertions with binding reverted in
# that sandbox's runtime; they must fail. Compatibility cases pair their
# unchanged behaviour with a stale-head probe, since old code also passes the
# compatibility assertion alone.
binding_refreshed() {
  [ "$LAST_RC" = 20 ] && [ "$OUTCOME" = refreshed ] && ! pr2_merged \
    && grep -q '^approval_binding: ' <<< "$LINES" \
    && grep -q $'move_issue\t'"$ISSUE"$'\tstate-build-review' "$SANDBOX/calls.log" \
    && ! grep -q $'add_issue_label\t'"$ISSUE"$'\tneeds-human' "$SANDBOX/calls.log" \
    && { [ ! -f "$SANDBOX/.git/bureau/merge-gate-waits.json" ] \
      || jq -e --arg i "$ISSUE" 'has($i) | not' "$SANDBOX/.git/bureau/merge-gate-waits.json" >/dev/null; }
}
binding_gate_comments() { jq '[.[] | select(.body | test("Bureau merge gate"))] | length' "$PR2_GH/comments.json"; }
binding_case() {
  local scenario="$1" control="$2" approved current reviewed_base
  new_sandbox
  case "$control" in
    reverted) pr2_revert_approval_binding ;;
    snapshot) pr2_revert_merge_base_fix snapshot ;;
  esac
  approved=$(jq -r .headRefOid "$PR2_GH/pr.json")
  case "$scenario" in
    dedicated)
      pr2_push_head
      current=$(jq -r .headRefOid "$PR2_GH/pr.json")
      gate; binding_refreshed || return 1
      grep -qF "approved head $approved differs from current head $current" <<< "$LINES" || return 1
      [ "$(binding_gate_comments)" = 1 ] || return 1
      gate; binding_refreshed || return 1
      [ "$(binding_gate_comments)" = 1 ] || return 1
      ;;
    jit)
      printf 'source "%s"\npr2_push_head\n' "$LIB_DIR/pr2-gate.sh" > "$SANDBOX/push-head.sh"
      echo "$SANDBOX/push-head.sh" > "$PR2_GH/after_initial_head"
      gate; binding_refreshed || return 1
      grep -q 'all gates PASS' <<< "$LAST_STDOUT" || return 1
      [ "$(pr2_gate_reads)" = 2 ] || return 1
      current=$(jq -r .headRefOid "$PR2_GH/pr.json")
      grep -qF "approved head $approved differs from current head $current" <<< "$LINES" || return 1
      ;;
    newest)
      pr2_push_head; pr2_approval
      # Deliberately reverse array order: createdAt, not list order, decides.
      jq --arg h "$approved" '. + [(.[] | .id="newest" | .createdAt="2026-09-29T09:01:00Z" |
          .body |= sub("\\*\\*Reviewed head\\*\\*: `[^`]+`"; "**Reviewed head**: `" + $h + "`"))] | reverse' \
        "$PR2_GH/comments.json" > "$PR2_GH/comments.tmp" && mv "$PR2_GH/comments.tmp" "$PR2_GH/comments.json"
      gate; binding_refreshed || return 1
      grep -qF "approved head $approved differs" <<< "$LINES" || return 1
      ;;
    retarget)
      git -C "$SANDBOX" push -q origin 'origin/main:refs/heads/release'
      jq '.baseRefName="release"' "$PR2_GH/pr.json" > "$PR2_GH/pr.tmp" && mv "$PR2_GH/pr.tmp" "$PR2_GH/pr.json"
      gate; binding_refreshed || return 1
      grep -q 'approved Target branch main differs from current base branch release' <<< "$LINES" || return 1
      ;;
    missing)
      jq '.[0].body |= (split("\n") | map(select(startswith("**Reviewed head**") | not)) | join("\n"))' \
        "$PR2_GH/comments.json" > "$PR2_GH/comments.tmp" && mv "$PR2_GH/comments.tmp" "$PR2_GH/comments.json"
      gate; binding_refreshed || return 1
      grep -q 'Reviewed head line missing' <<< "$LINES" || return 1
      ;;
    matching|advanced-base|auto-approve)
      [ "$scenario" != auto-approve ] || pr2_approval AUTO_APPROVE
      if [ "$scenario" = advanced-base ]; then
        reviewed_base=$(jq -r .baseRefOid "$PR2_GH/pr.json")
        current=$(git -C "$SANDBOX" rev-parse HEAD)
        [ "$current" != "$reviewed_base" ] || return 1
        git -C "$SANDBOX" push -q origin 'HEAD:refs/heads/main'
        jq --arg h "$current" '.baseRefOid=$h' "$PR2_GH/pr.json" > "$PR2_GH/pr.tmp" && mv "$PR2_GH/pr.tmp" "$PR2_GH/pr.json"
        jq -n --arg h "$current" '{commit:{sha:$h}}' > "$PR2_GH/branch.json"
      fi
      gate
      [ "$LAST_RC" = 0 ] && pr2_merged || return 1
      grep -q -- '--match-head-commit'$'\t'"$approved" "$SANDBOX/gh_calls.log" || return 1
      if [ "$scenario" = advanced-base ]; then
        grep -qF "Target**: \`main\` at \`$reviewed_base\`" "$PR2_GH/comments.json" || return 1
        ! grep -q $'\t--json\tbaseRefOid\t' "$SANDBOX/gh_calls.log" || return 1
      fi
      # The same approval must stop covering the next pushed commit.
      pr2_push_head; gate; binding_refreshed || return 1
      ;;
    comments-read|head-read|metadata-read)
      pr2_push_head
      case "$scenario" in
        comments-read) touch "$PR2_GH/fail_comments_read" ;;
        head-read) touch "$PR2_GH/fail_head_read" ;;
        metadata-read) touch "$PR2_GH/fail_comment_metadata" ;;
      esac
      gate
      [ "$LAST_RC" = 2 ] && [ "$OUTCOME" = not-yet ] && ! pr2_merged || return 1
      ! grep -qE '^(move_issue|add_issue_label)' "$SANDBOX/calls.log" || return 1
      grep -qE '^(verdict_read|head_read): ' <<< "$LINES" || return 1
      if [ "$scenario" = comments-read ]; then
        pr2_checks red; gate
        [ "$LAST_RC" = 25 ] && [ "$OUTCOME" = blocked ] && ! pr2_merged || return 1
        grep -q '^verdict_read: ' <<< "$LINES" && grep -q '^ci_green: ci: failing check' <<< "$LINES" || return 1
        pr2_checks green
      fi
      rm -f "$PR2_GH/fail_comments_read" "$PR2_GH/fail_head_read" "$PR2_GH/fail_comment_metadata"
      gate; binding_refreshed || return 1
      ;;
  esac
}
for scenario in dedicated jit newest retarget missing matching advanced-base auto-approve comments-read head-read metadata-read; do
  binding_case "$scenario" current || fail "binding $scenario: rc $LAST_RC outcome $OUTCOME: $LINES"
  if binding_case "$scenario" reverted; then fail "binding $scenario: negative control stayed green"; fi
  [ "$LAST_RC" = 0 ] && pr2_merged || fail "binding $scenario: negative control failed before the unsafe merge"
  echo "PASS binding $scenario; identical assertions reject the private copy with binding reverted"
done
if binding_case advanced-base snapshot; then fail 'advanced base: split base reads stayed green'; fi
[ "$LAST_RC" = 0 ] && pr2_merged && grep -q $'\t--json\tbaseRefOid\t' "$SANDBOX/gh_calls.log" \
  || fail 'advanced base: split-read control did not re-read the PR base OID'
echo 'PASS advanced base: the approved branch remains eligible with a newer SHA; split-read control takes the OID from a separate read'

# Observe both the routed outcome and the exact branch used for freshness.
# Retarget either after the second gate snapshot or during merge-message
# preparation after both passes. The PR's head stays the same.
retarget_refreshed() {
  binding_refreshed \
    && grep -q 'approved Target branch main differs from current base branch release' <<< "$LINES" \
    && ! grep -q $'^gh\tpr\tmerge\t' "$SANDBOX/gh_calls.log" \
    && [ "$(binding_gate_comments)" = 1 ]
}
retarget_case() {
  local read_kind="$1" control="$2" read_count=1
  new_sandbox
  [ "$control" = current ] || pr2_revert_merge_base_fix "$control"
  [ "$read_kind" != gate-snapshot ] || read_count=2
  pr2_retarget_after_read "$read_kind" "$read_count"
  gate
  retarget_refreshed || return 1
  if [ "$read_kind" = merge-message ]; then
    [ "$(grep -c '/branches/main' "$SANDBOX/gh_calls.log")" = 1 ] || return 1
    [ "$(grep -c '/branches/release' "$SANDBOX/gh_calls.log")" = 1 ] || return 1
  else
    [ "$(grep -c '/branches/main' "$SANDBOX/gh_calls.log")" = 2 ] || return 1
    ! grep -q '/branches/release' "$SANDBOX/gh_calls.log" || return 1
  fi
  gate; retarget_refreshed
}
for scenario in gate-snapshot merge-message; do
  retarget_case "$scenario" current || fail "retarget after $scenario: rc $LAST_RC outcome $OUTCOME: $LINES"
  if [ "$scenario" = merge-message ]; then
    echo 'PASS retarget during message preparation is seen by the final gate snapshot'
    continue
  fi
  control=target
  [ "$scenario" != gate-snapshot ] || control=both
  if retarget_case "$scenario" "$control"; then fail "retarget after $scenario: negative control stayed green"; fi
  [ "$LAST_RC" = 0 ] && pr2_merged && [ "$(jq -r .baseRefName "$PR2_GH/pr.json")" = release ] \
    || fail "retarget after $scenario: negative control failed before the unsafe merge"
  echo "PASS retarget after $scenario: 20/refreshed once; private reverted copy merges into release"
done
if retarget_case gate-snapshot snapshot; then fail 'retarget during the final gate: split base reads stayed green'; fi
retarget_refreshed && grep -q '/branches/release' "$SANDBOX/gh_calls.log" \
  || fail 'retarget during the final gate: the split-read control did not judge the wrong branch'
echo 'PASS retarget during the final gate: freshness uses the same base as approval binding; split-read control judges release'

# The late check runs inside the merge conditional. A failed Linear transition
# there still propagates its error and cannot announce a completed handoff.
late_transition_failure() {
  local control="$1"
  new_sandbox
  pr2_retarget_after_read gate-snapshot 2
  printf '\nmove_issue() { return 27; }\n' >> "$SCRIPTS_DIR/bureau-config.sh"
  if [ "$control" = reverted ]; then
    python3 - "$SCRIPTS_DIR/merge-pipeline.sh" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); source = p.read_text()
move = '  move_issue "$ISSUE" "$BUREAU_STATE_BUILD_REVIEW"'
assert source.count(move + ' || exit $?\n') == 1
p.write_text(source.replace(move + ' || exit $?\n', move + '\n'))
PY
  fi
  gate
  [ "$LAST_RC" = 27 ] && [ -z "$OUTCOME" ] && ! pr2_merged && [ "$(binding_gate_comments)" = 0 ]
}
late_transition_failure current || fail "late transition failure: rc $LAST_RC outcome $OUTCOME: $LINES"
if late_transition_failure reverted; then fail 'late transition failure: negative control stayed green'; fi
[ "$LAST_RC" = 20 ] && [ "$OUTCOME" = refreshed ] && ! pr2_merged && [ "$(binding_gate_comments)" = 1 ] \
  || fail 'late transition failure: reverted copy did not falsely announce a handoff'
echo 'PASS late transition failure: propagates exit 27; private reverted copy announces a handoff'

live_base_read_failure() {
  gate
  [ "$LAST_RC" = 2 ] && [ "$OUTCOME" = not-yet ] && ! pr2_merged \
    && grep -q '^pr_read: .*base branch could not be read before merge' <<< "$LINES" \
    && ! grep -qE '^(move_issue|add_issue_label)' "$SANDBOX/calls.log"
}
new_sandbox
touch "$PR2_GH/fail_live_base_read"
live_base_read_failure || fail "final base read failure: rc $LAST_RC outcome $OUTCOME: $LINES"
pr2_revert_merge_base_fix target
if live_base_read_failure; then fail 'final base read failure: negative control stayed green'; fi
[ "$LAST_RC" = 0 ] && pr2_merged || fail 'final base read failure: reverted copy did not merge'
echo 'PASS failed final base read: 2/not-yet; private reverted copy merges'

# A failed cosmetic read cannot hide a confirmed stale approval or its handoff.
dedup_read_failure() {
  new_sandbox
  pr2_push_head
  pr2_checks red
  touch "$PR2_GH/fail_gate_comment_read"
  if [ "$1" = reverted ]; then
    python3 - "$SCRIPTS_DIR/merge-pipeline.sh" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
source = path.read_text()
start = source.index('  last_body=$(gh pr view', source.index('refresh_stale_approval() {'))
end = source.index('\n', source.index(' || last_body=""', start))
read = source[start:end].removesuffix(' || last_body=""')
assert source[start:end] != read
path.write_text(source[:start] + '  if ! ' + read.lstrip() + '''; then
    merge_gate_exit not-yet "verdict_read: the PR's review comments could not be read"
  fi''' + source[end:])
PY
  fi
  gate
  binding_refreshed && grep -q '^ci_green: ci: failing check' <<< "$LINES" \
    && grep -q 'HTTP 502 (gate comment lookup)' <<< "$LAST_STDERR" \
    && [ "$(binding_gate_comments)" = 1 ]
}
dedup_read_failure current || fail "failed gate comment lookup: rc $LAST_RC outcome $OUTCOME: $LINES"
if dedup_read_failure reverted; then fail 'failed gate comment lookup: negative control stayed green'; fi
[ "$LAST_RC" = 2 ] && [ "$OUTCOME" = not-yet ] && ! pr2_merged \
  && grep -q '^verdict_read:' <<< "$LINES" && grep -q 'ci_green: ci: failing check' <<< "$LAST_STDOUT" \
  && ! grep -q '^move_issue' "$SANDBOX/calls.log" \
  || fail 'failed gate comment lookup: the old code did not mask a confirmed stale approval'
echo 'PASS stale approval with red CI and failed dedup read still hands back to Build Review and posts; reverted code exits 2'

# Exact writer syntax and unedited identity are required even when all hashes
# happen to name the current commit. No ambiguous comment can supply a binding.
for malformed in duplicate-head duplicate-target malformed-extra-target missing-target short-head bad-target-sha whitespace; do
  new_sandbox
  python3 - "$PR2_GH/comments.json" "$malformed" <<'PY'
import json, sys
from pathlib import Path
p=Path(sys.argv[1]); rows=json.loads(p.read_text()); row=rows[0]; lines=row['body'].splitlines(); kind=sys.argv[2]
head=next(s for s in lines if s.startswith('**Reviewed head**'))
target=next(s for s in lines if s.startswith('**Target**'))
if kind=='duplicate-head': lines.append(head)
elif kind=='duplicate-target': lines.append(target)
elif kind=='malformed-extra-target': lines.append('**Target**: malformed')
elif kind=='missing-target': lines.remove(target)
elif kind=='short-head': lines[lines.index(head)]=head[:-2]+'`'
elif kind=='bad-target-sha': lines[lines.index(target)]=target[:-2]+'z`'
elif kind=='whitespace': lines[lines.index(head)]=head+' '
row['body']='\n'.join(lines); p.write_text(json.dumps(rows))
PY
  gate; binding_refreshed || fail "strict binding $malformed: rc $LAST_RC: $LINES"
  pr2_revert_approval_binding
  gate
  [ "$LAST_RC" = 0 ] && pr2_merged || fail "strict binding $malformed: negative control did not merge"
  echo "PASS strict binding $malformed; private reverted copy merges it"
done

# The node edit timestamp is required even when the body and binding are unchanged.
# Ignoring timestamps while comparing identical node bodies permits the unsafe merge.
new_sandbox
jq '.[0].lastEditedAt = "2026-09-29T09:01:00Z"' "$PR2_GH/comments.json" > "$PR2_GH/comments.tmp"
mv "$PR2_GH/comments.tmp" "$PR2_GH/comments.json"
gh pr view 99 --json comments | jq -e '.comments[0] | .includesCreatedEdit and (has("lastEditedAt") | not)' >/dev/null \
  || fail 'edited approval: fake gh exposed the wrong projection'
edited_approval_refreshed() {
  gate
  binding_refreshed && grep -q 'selected review was edited' <<< "$LINES"
}
edited_approval_refreshed || fail "edited approval: rc $LAST_RC outcome $OUTCOME: $LINES"
python3 - "$SCRIPTS_DIR/bureau-merge-review.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
source = path.read_text()
old = "selected['lastEditedAt'] is not None or reread['lastEditedAt'] is not None or reread != selected"
assert source.count(old) == 1
path.write_text(source.replace(old, 'reread != selected'))
PY
if edited_approval_refreshed; then fail 'edited approval: the ignored timestamp mutation stayed green'; fi
[ "$LAST_RC" = 0 ] && pr2_merged || fail 'edited approval: ignored timestamp mutation did not reach the unsafe merge'
echo 'PASS edited approval requires fresh review; ignoring timestamps lets it merge in the private copy'

echo 'OK test_merge_gate_switches'
