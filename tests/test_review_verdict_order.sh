#!/bin/bash
# The review stage's verdict rules run in one order: verdict check, security count, the
# security specialist's CRITICAL count, security floor, build fold, cycle cap last.
#
# Two defects came from the old order and the old defaults (EXP-1514, EXP-1518 in
# slidefactory-core, same lines in the template):
#   - the cycle cap ran before the build fold, so "reviewers approve, build red" reached
#     the cap as APPROVE, was folded to REQUEST_CHANGES afterwards and went round forever;
#   - the security floor read a missing or unreadable security_issues as 0, and raised
#     only APPROVE, so a CRITICAL security finding under a REQUEST_CHANGES header went
#     into autonomous rework.
#
# Part 1 runs the REAL decide_review_verdict from templates/scripts/bureau-config.sh on a
# table. Part 2 runs the REAL code-review-pipeline.sh end to end in the harness sandbox
# (stub gh, stub Linear, fake reviewers) for the ticket scenarios, and puts the old
# verdict block back into the sandbox copy to show the same assertions turn on it.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP

SCRIPTS="$REPO_ROOT/templates/scripts"
fail() { echo "FAIL $*" >&2; printf '%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }

# --- Part 1: the decision on its own ------------------------------------------------
FNS=$(mktemp -t bureau-test.verdictfns.XXXXXXXX)
MARKS=$(mktemp -d -t bureau-test.verdict.XXXXXXXX)
trap 'teardown || true; rm -rf "$FNS" "$MARKS"' EXIT
sed -n -e '/^apply_build_failure() {/,/^}/p' -e '/^decide_review_verdict() {/,/^}/p' \
  "$SCRIPTS/bureau-config.sh" > "$FNS"
grep -q '^decide_review_verdict() {' "$FNS" || fail "decide_review_verdict is not in bureau-config.sh"

# decide <verdict> <security_issues> <critical> <build_ok> <cycles> <max> → "VERDICT rule,rule"
decide() {
  /bin/bash -c 'set -euo pipefail; source "$1"; shift; decide_review_verdict "$@"' _ "$FNS" "$@" \
    | awk -F'\037' 'NR==1{v=$0; next} {r=r (r?",":"") $1} END{print v " " r}'
}
expect() {  # expect "<verdict> <rules>" <args...>
  local want="$1"; shift
  local got; got=$(decide "$@")
  [ "$got" = "$want" ] || fail "decide_review_verdict $* gave '$got', wanted '$want'"
}

# The ticket rows.
expect "BLOCK build,cycle-cap"          APPROVE 0 0 false 7 3    # EXP-1514: approve + red build at the cap
expect "BLOCK build,cycle-cap"          APPROVE 0 0 false 3 3    # exactly at the cap
expect "REQUEST_CHANGES build"          APPROVE 0 0 false 2 3    # below the cap: rework
expect "BLOCK security-critical"        REQUEST_CHANGES 2 2 true 0 3   # EXP-1518: CRITICAL under a REQUEST_CHANGES header
expect "BLOCK security-critical"        APPROVE 2 1 true 0 3
expect "REQUEST_CHANGES "               REQUEST_CHANGES 2 0 true 0 3   # non-critical security bug: rework
expect "REQUEST_CHANGES security-floor" APPROVE 1 0 true 0 3
expect "BLOCK security-unreadable"      APPROVE -1 0 true 0 3    # EXP-1518: -1 used to become 0
expect "BLOCK security-unreadable"      APPROVE high 0 true 0 3
expect "BLOCK security-unreadable"      APPROVE "" 0 true 0 3    # missing field
expect "BLOCK security-unreadable"      APPROVE null 0 true 0 3
# The specialist's own count is free text: unreadable is noted, never escalated.
expect "APPROVE security-critical-unknown" APPROVE 0 "" true 0 3
expect "APPROVE security-critical-unknown" APPROVE 0 -2 true 0 3
# Verdicts the table does not know, and a cap that cannot be read.
expect "BLOCK verdict"                  approve 0 0 true 0 3
expect "BLOCK verdict"                  "" 0 0 true 0 3
expect "BLOCK cycle-cap"                REQUEST_CHANGES 0 0 true "" 3
expect "BLOCK cycle-cap"                REQUEST_CHANGES 0 0 true 1 x
expect "APPROVE "                       APPROVE 0 0 true "" 3    # the cap is for REQUEST_CHANGES only
# A build that is not reported green folds too.
expect "REQUEST_CHANGES build"          APPROVE 0 0 "" 0 3
echo "PASS the ticket rows and the fall-closed rows"

# Properties over the whole grid: verdict × security count × build × cycles (critical 0).
n=0
for v in APPROVE REQUEST_CHANGES BLOCK; do
  for s in 0 1 -1 garbage ""; do
    for b in true false; do
      for c in 0 2 3 9; do
        out=$(decide "$v" "$s" 0 "$b" "$c" 3); got="${out%% *}"; n=$((n + 1))
        case "$s" in 0|1) readable=1 ;; *) readable=0 ;; esac
        [ "$readable" = 1 ] || [ "$got" = BLOCK ] || fail "unreadable security '$s' gave $got ($v $b $c)"
        [ "$v" != BLOCK ] || [ "$got" = BLOCK ] || fail "a BLOCK became $got ($s $b $c)"
        [ "$s" != 1 ] || [ "$got" != APPROVE ] || fail "a security finding was approved ($v $b $c)"
        [ "$b" = true ] || [ "$got" != APPROVE ] || fail "a red build was approved ($v $s $c)"
        [ "$got" != REQUEST_CHANGES ] || [ "$c" -lt 3 ] || fail "REQUEST_CHANGES at cycle $c of 3 ($v $s $b)"
        if [ "$readable" = 1 ] && [ "$c" -ge 3 ] && [ "$v" != BLOCK ] && { [ "$b" = false ] || [ "$v" = REQUEST_CHANGES ] || [ "$s" = 1 ]; }; then
          [ "$got" = BLOCK ] || fail "a rework verdict at cycle $c of 3 was not escalated ($v $s $b → $got)"
        fi
        if [ "$s" = 0 ] && [ "$b" = true ] && { [ "$v" != REQUEST_CHANGES ] || [ "$c" -lt 3 ]; }; then
          [ "$got" = "$v" ] || fail "a clean review changed $v into $got (cycle $c)"
        fi
      done
    done
  done
done
echo "PASS $n grid cases hold the six rules"

# --- Part 2: the real stage ---------------------------------------------------------
# The old verdict block (template main before this change), for the negative control.
OLD_BLOCK=$(cat <<'OLD'
_sec_issues=$(parse_claude_json "$MERGED_REVIEW" '.security_issues // 0')
[[ "$_sec_issues" =~ ^[0-9]+$ ]] || _sec_issues=0
if [ "$_sec_issues" -gt 0 ] && [ "$VERDICT" = "APPROVE" ]; then
  VERDICT="REQUEST_CHANGES"
fi
MAX_REVIEW_CYCLES="$BUREAU_MAX_REVIEW_CYCLES"
if [ "$VERDICT" = "REQUEST_CHANGES" ] && [ "${REVIEW_CYCLE_COUNT:-0}" -ge "$MAX_REVIEW_CYCLES" ]; then
  VERDICT="BLOCK"
  ESCALATION_REASON="REQUEST_CHANGES exceeded max_review_cycles=$MAX_REVIEW_CYCLES"
  MERGED_REVIEW="$MERGED_REVIEW

**ESCALATED:** $REVIEW_CYCLE_COUNT review cycles (max $MAX_REVIEW_CYCLES). Needs human intervention."
fi
if [ "$BUILD_OK" = false ]; then
  VERDICT=$(apply_build_failure "$VERDICT")
  MERGED_REVIEW="$MERGED_REVIEW

BUILD FAILURE: Must be fixed."
fi
OLD
)
export OLD_BLOCK

# $1 verdict, $2 security_issues ('-' = field missing), $3 specialist CRITICAL count,
# $4 build: green | red, $5 prior "Changes Requested" comments, $6 = old (optional).
# Leaves LAST_*, $VERDICT_LINE and $POSTED (everything posted to the PR).
run_case() {
  local verdict="$1" sec="$2" crit="$3" build="$4" prior="$5" old="${6:-}"
  sandbox_init EXP-702 test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  local cmd="true"; [ "$build" = green ] || cmd="exit 3"
  jq -n --arg c "$cmd" '{repo: {test_command: $c}}' > "$SANDBOX/.bureau.json"
  if [ "$old" = oldread ]; then
    python3 - "$SCRIPTS_DIR/code-review-pipeline.sh" <<'PY'
import sys
path = sys.argv[1]
src = open(path).read()
start = src.index('_comments_rc=0\n')
end = src.index('\nfi\n', src.index('if ! [[ "$REVIEW_CYCLE_COUNT" =~')) + len('\nfi\n')
old = 'REVIEW_CYCLE_COUNT=$(get_issue_comments "$ISSUE" \\\n  | jq \'[.[] | select(.body | test("Code Review.*Changes Requested"))] | length\' 2>/dev/null || echo "0")\n'
src = src[:start] + old + src[end:]
open(path, 'w').write(src)
PY
  fi
  if [ "$old" = old ]; then
    python3 - "$SCRIPTS_DIR/code-review-pipeline.sh" <<'PY'
import os, sys
path = sys.argv[1]
src = open(path).read()
start = src.index('# The verdict rules run as one ordered decision')
end_marker = 'done <<< "$(printf \'%s\\n\' "$_decision" | tail -n +2)"\n'
end = src.index(end_marker, start) + len(end_marker)
src = src[:start] + os.environ['OLD_BLOCK'] + '\n' + src[end:]
open(path, 'w').write(src)
PY
  fi
  # One fixture answers every reviewer call; its last json block is read both as the
  # merged verdict and as the security specialist's own counts.
  jq -n --arg v "$verdict" --arg s "$sec" --argjson c "$crit" \
    '{verdict:$v, bugs:0, security_issues:(if $s == "-" then null else ($s|tonumber) end),
      counts:{critical:$c}, findings:[], summary:"fixture"} | if .security_issues == null then del(.security_issues) else . end' \
    > "$SANDBOX/verdict.json"
  { printf 'Review.\n```json\n'; cat "$SANDBOX/verdict.json"; printf '```\n'; } > "$SANDBOX/verdict.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/verdict.txt"
  local comments='[]' i
  for i in $(seq 1 "$prior"); do
    comments=$(jq -c --arg b "🔄 Code Review: **Changes Requested** (cycle $((i - 1))/3)" '. + [{body:$b, createdAt:"2026-09-28T00:00:00Z"}]' <<< "$comments")
  done
  export BUREAU_STUB_REVIEW_COMMENTS="$comments"
  export BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  export BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=0
  [ "$old" != hold ] || return 0
  run_pipeline code-review-pipeline.sh EXP-702 </dev/null
  rm -rf "$(printf '%s\n' "$LAST_STDERR" | sed -n 's/^code-review failed .*preserved at //p')"
  POSTED=$(cat "$SANDBOX/gh_calls.log" 2>/dev/null || true)
  VERDICT_LINE=$(grep -E '^\*\*Verdict\*\*: ' <<< "$POSTED" || true)
}

# EXP-1514: reviewers approve, the build stays red, three rework cycles already posted.
run_case APPROVE 0 0 red 3
[ "$VERDICT_LINE" = '**Verdict**: BLOCK' ] || fail "approve + red build at the cap posted '$VERDICT_LINE'"
[ "$LAST_RC" = 25 ] || fail "approve + red build at the cap ended with $LAST_RC, wanted 25"
assert_calls_include 'add_issue_label.*needs-human' 'the escalation labels needs-human'
assert_calls_exclude 'move_issue' 'an escalated ticket is not sent back to Build'
grep -q 'only the build check stayed red' <<< "$POSTED" || fail "the review does not say the build alone kept the loop going"
grep -q 'cannot tell a failure caused by the code from one caused by the environment' <<< "$POSTED" \
  || fail "the review does not say the pipeline cannot tell code from environment"
grep -qF 'reason="REQUEST_CHANGES exceeded max_review_cycles=3 (reviewers approved, build red)"' "$SANDBOX/logs/escalations.log" \
  || fail "the escalation log does not carry the cap reason: $(cat "$SANDBOX/logs/escalations.log" 2>/dev/null)"
teardown
run_case APPROVE 0 0 red 3 old
[ "$VERDICT_LINE" = '**Verdict**: REQUEST_CHANGES' ] || fail "negative control: the old order no longer loops ('$VERDICT_LINE')"
teardown
echo "PASS approve + red build escalates at the cap (the old order sent it round again)"

run_case APPROVE 0 0 red 1
[ "$VERDICT_LINE" = '**Verdict**: REQUEST_CHANGES' ] || fail "approve + red build below the cap posted '$VERDICT_LINE'"
[ "$LAST_RC" = 0 ] || fail "rework below the cap ended with $LAST_RC"
assert_calls_include 'move_issue' 'rework goes back to Build'
teardown
echo "PASS below the cap a red build is rework"

# EXP-1518: a CRITICAL security finding under a REQUEST_CHANGES header.
run_case REQUEST_CHANGES 2 2 green 0
[ "$VERDICT_LINE" = '**Verdict**: BLOCK' ] || fail "CRITICAL under REQUEST_CHANGES posted '$VERDICT_LINE'"
[ "$LAST_RC" = 25 ] || fail "CRITICAL under REQUEST_CHANGES ended with $LAST_RC"
teardown
run_case REQUEST_CHANGES 2 2 green 0 old
[ "$VERDICT_LINE" = '**Verdict**: REQUEST_CHANGES' ] || fail "negative control: the old floor now escalates ('$VERDICT_LINE')"
teardown
echo "PASS a CRITICAL security finding needs a human (the old floor sent it into rework)"

# The CRITICAL count is the security specialist's own, not the merger's: the merger says
# REQUEST_CHANGES with 2 security findings and nothing critical, the specialist says 2
# CRITICAL — and the other way round.
role_case() {  # $1 = merger's critical count, $2 = specialist's critical count
  run_case REQUEST_CHANGES 2 0 green 0 hold
  printf 'Merged.\n```json\n{"verdict":"REQUEST_CHANGES","bugs":0,"security_issues":2,"counts":{"critical":%s},"summary":"m"}\n```\n' "$1" > "$SANDBOX/merge.txt"
  printf 'Security.\n```json\n{"specialist":"security","counts":{"critical":%s,"bug":2,"minor":0,"skip":0},"findings":[],"summary":"s"}\n```\n' "$2" > "$SANDBOX/security.txt"
  FAKE_CLAUDE_MERGE_FIXTURE="$SANDBOX/merge.txt" FAKE_CLAUDE_SECURITY_FIXTURE="$SANDBOX/security.txt" \
    run_pipeline code-review-pipeline.sh EXP-702 </dev/null
  POSTED=$(cat "$SANDBOX/gh_calls.log" 2>/dev/null || true)
  VERDICT_LINE=$(grep -E '^\*\*Verdict\*\*: ' <<< "$POSTED" || true)
}
role_case 0 2
[ "$VERDICT_LINE" = '**Verdict**: BLOCK' ] || fail "the specialist's CRITICAL count was not read ('$VERDICT_LINE')"
teardown
role_case 2 0
[ "$VERDICT_LINE" = '**Verdict**: REQUEST_CHANGES' ] || fail "a CRITICAL count in the merger's block was taken for the specialist's ('$VERDICT_LINE')"
teardown
echo "PASS the CRITICAL count comes from the security specialist's own block"

# EXP-1518: an unreadable or missing security count is not "none".
for sec in -1 -; do
  run_case APPROVE "$sec" 0 green 0
  [ "$VERDICT_LINE" = '**Verdict**: BLOCK' ] || fail "security_issues '$sec' posted '$VERDICT_LINE'"
  [ "$LAST_RC" = 25 ] || fail "security_issues '$sec' ended with $LAST_RC"
  grep -q 'SECURITY COUNT UNREADABLE' <<< "$POSTED" || fail "security_issues '$sec' is not named in the review"
  teardown
  run_case APPROVE "$sec" 0 green 0 old
  [ "$VERDICT_LINE" = '**Verdict**: APPROVE' ] || fail "negative control: the old floor no longer reads '$sec' as 0 ('$VERDICT_LINE')"
  teardown
done
echo "PASS an unreadable or missing security count blocks (the old floor approved)"

# Two escalating rules: the first one (the root cause) is the escalation-log reason.
run_case MAYBE - 0 green 0
[ "$VERDICT_LINE" = '**Verdict**: BLOCK' ] || fail "an unknown verdict posted '$VERDICT_LINE'"
grep -q 'VERDICT UNREADABLE' <<< "$POSTED" && grep -q 'SECURITY COUNT UNREADABLE' <<< "$POSTED" \
  || fail "both escalating rules should be named in the review"
grep -qF 'reason="review verdict unreadable"' "$SANDBOX/logs/escalations.log" \
  || fail "the escalation log does not name the first rule: $(cat "$SANDBOX/logs/escalations.log" 2>/dev/null)"
teardown
echo "PASS the first escalating rule is the logged reason, every rule is in the review"

# Control: a clean review is untouched.
run_case APPROVE 0 0 green 0
[ "$VERDICT_LINE" = '**Verdict**: APPROVE' ] || fail "a clean review posted '$VERDICT_LINE'"
[ "$LAST_RC" = 20 ] || fail "a clean approval should stop before merge with 20, got $LAST_RC"
teardown
echo "PASS a clean review is approved as before"

# The cycle count is read before any paid review, and a failed read is not cycle 0.
for pair in 27:27 10:10 5:27; do
  rc_in="${pair%%:*}"; rc_want="${pair##*:}"
  export BUREAU_STUB_REVIEW_COMMENTS_RC="$rc_in"
  run_case REQUEST_CHANGES 0 0 green 0
  unset BUREAU_STUB_REVIEW_COMMENTS_RC
  [ "$LAST_RC" = "$rc_want" ] || fail "a comment read failing with $rc_in ended with $LAST_RC, wanted $rc_want"
  [ ! -e "$SANDBOX/fake_claude_counter" ] || fail "a failed comment read still ran the paid review (exit $rc_in)"
  [ -z "$VERDICT_LINE" ] || fail "a failed comment read still posted a verdict"
  teardown
done
export BUREAU_STUB_REVIEW_COMMENTS_RC=27
run_case REQUEST_CHANGES 0 0 green 0 oldread
unset BUREAU_STUB_REVIEW_COMMENTS_RC
[ -e "$SANDBOX/fake_claude_counter" ] || fail "negative control: the old read no longer runs the review after a failed read"
teardown
echo "PASS a failed cycle-count read stops the stage before the paid review (the old read counted it as cycle 0)"
