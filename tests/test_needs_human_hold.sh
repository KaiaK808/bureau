#!/bin/bash
# A needs-human escalation survives a label write that fails (EXP-1516, EXP-1482 path 6).
#
# Part 1 runs the REAL mark_needs_human and pipeline_pick_next/pick_issue from
# templates/scripts/bureau-config.sh under /bin/bash against a stubbed `curl`: the label
# lookup answers with the label, with none (a plain failure) or with HTML (Linear unusable,
# 27); `adderr` fails only the label mutation (GraphQL errors), `down` fails every call; the
# issue list holds EXP-1 (older, higher priority) and EXP-2, and EXP-1 carries needs-human
# once a label mutation went out. Part 3 runs QA's cleanup trap and queue-loop's run_script,
# both cut from the real scripts.
# Part 2 runs the needs-human arms cut out of the REAL stage scripts with the harness's stub
# config (its add_issue_label returns BUREAU_STUB_ADD_LABEL_RC) and the real hold helpers.
# The arms are cut by their first line and checked by behaviour, not by spelling.
# Negative controls run the picker and the rebase/merge arms in their form before the change
# (reconstructed here, because CI checks out without history): the picker takes the held
# ticket again, and the arms end with 0 (rebase) or 18 (merge) whatever the label write did.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

SCRIPTS="$REPO_ROOT/templates/scripts"
fail() { echo "FAIL $*" >&2; printf '  | rc=%s out=%s\n  | err=%s\n' "${RC:-}" "${OUT:-}" "${ERR:-}" >&2; exit 1; }

SB=$(mktemp -d -t bureau-test.nhhold.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
git -C "$SB" init -q
HOLD="$SB/.git/bureau/needs-human-held"
# The picker as it was: the hold lines in pipeline_pick_next taken out again.
mkdir -p "$SB/old"; cp "$SCRIPTS"/*.sh "$SB/old/"
python3 - "$SCRIPTS/bureau-config.sh" "$SB/old/bureau-config.sh" <<'PYOLD'
import re, sys
s = open(sys.argv[1]).read()
new = re.search(r"\n  # A ticket whose needs-human label could not be written is held locally\n.*?\n  if \[ -n \"\$skip\" \]; then\n    pick_issue \"\$state\" \"\$required\" \"\$exclude\" \"\$skip\"\n", s, re.S)
assert new, "the hold lines in pipeline_pick_next are not where they were"
old = '\n  if [ -n "${2:-}" ]; then\n    pick_issue "$state" "$required" "$exclude" "$2"\n'
open(sys.argv[2], "w").write(s[:new.start()] + old + s[new.end():])
PYOLD

cat > "$SB/.bureau.json" <<'EOF'
{
  "linear": {
    "teams": [{
      "id": "team-id", "key": "EXP", "name": "Test",
      "states": {
        "triage": "s1", "spec": "s2", "spec_review": "s3", "design": "s4",
        "build": "s5", "build_review": "s6", "done": "s7"
      }
    }],
    "labels": {
      "lane2":            { "id": "l1", "name": "lane-2" },
      "needs_human":      { "id": "l2", "name": "needs-human" },
      "needs_ux":         { "id": "l3", "name": "needs-ux" },
      "ai_implementable": { "id": "l4", "name": "ai-implementable" }
    },
    "projects": []
  },
  "agents": { "poll_interval_minutes": 30, "max_review_cycles": 3 },
  "repo": { "branch_prefix": "feat", "specs_dir": "specs" }
}
EOF

mkdir -p "$SB/bin"
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
prev=""; payload=""
for a in "\$@"; do [ "\$prev" = "-d" ] && payload="\$a"; prev="\$a"; done
mode=\$(cat "$SB/mode")
[ "\$mode" = down ] && { echo down >> "$SB/linear.log"; echo '<html>502</html>'; exit 0; }
case "\$payload" in
  *issueAddLabel*)
    echo add >> "$SB/linear.log"
    [ "\$mode" = adderr ] && { echo '{"errors":[{"message":"internal"}]}'; exit 0; }
    touch "$SB/labelled"
    echo '{"data":{"issueAddLabel":{"success":true}}}' ;;
  *issueLabels*)
    echo lookup >> "$SB/linear.log"
    case "\$mode" in
      broken) echo '<html>502</html>' ;;
      none)   echo '{"data":{"issueLabels":{"nodes":[]}}}' ;;
      *)      echo '{"data":{"issueLabels":{"nodes":[{"id":"L-EXP","team":{"key":"EXP"}}]}}}' ;;
    esac ;;
  *inverseRelations*)
    echo list >> "$SB/linear.log"
    extra=""; [ -f "$SB/labelled" ] && extra=',{"name":"needs-human"}'
    printf '{"data":{"issues":{"nodes":[%s,%s]}}}' \
      "{\"identifier\":\"EXP-1\",\"priority\":1,\"createdAt\":\"2026-01-01\",\"labels\":{\"nodes\":[{\"name\":\"lane-2\"}\$extra]},\"inverseRelations\":{\"nodes\":[]}}" \
      '{"identifier":"EXP-2","priority":2,"createdAt":"2026-01-02","labels":{"nodes":[{"name":"lane-2"}]},"inverseRelations":{"nodes":[]}}' ;;
  *) echo uuid >> "$SB/linear.log"; echo '{"data":{"issues":{"nodes":[{"id":"ISSUE-UUID"}]}}}' ;;
esac
EOF
printf '#!/bin/bash\n:\n' > "$SB/bin/sleep"
chmod +x "$SB/bin/curl" "$SB/bin/sleep"

# $1 = linear mode (ok|none|broken), $2 = snippet, $3 = config (default: the real one)
real() {
  printf '%s' "$1" > "$SB/mode"; rm -f "$SB/linear.log" "$SB/labelled"
  set +e
  OUT=$(cd "$SB" && PATH="$SB/bin:$PATH" LINEAR_API_KEY=k BUREAU_LINEAR_RETRIES=0 /bin/bash -c "
    set -uo pipefail
    source '${3:-$SCRIPTS/bureau-config.sh}'
    alert_telegram() { echo \"ALERT \$*\" >&2; }
    $2" 2>"$SB/err")
  RC=$?
  set -e
  ERR=$(cat "$SB/err")
  CALLS=""; if [ -f "$SB/linear.log" ]; then CALLS=$(tr '\n' ' ' < "$SB/linear.log"); fi
}
hold() { mkdir -p "$HOLD"; printf 'stage=test\texit=1\tat=x\n' > "$HOLD/$1"; }

# --- the helper ---------------------------------------------------------------------
hold EXP-1
real ok 'mark_needs_human EXP-1 code-review'
[ "$RC" = 0 ] || fail "a label that is written returns 0"
[ ! -e "$HOLD/EXP-1" ] || fail "a written label leaves the old hold behind"
case "$ERR" in *ALERT*) fail "a written label alerts" ;; esac

real none 'mark_needs_human EXP-1 code-review'
[ "$RC" = 1 ] || fail "a label that cannot be written must not return 0"
grep -q "^stage=code-review	exit=1	at=" "$HOLD/EXP-1" 2>/dev/null || fail "no hold recorded for EXP-1"
case "$ERR" in *"held in $HOLD/EXP-1"*) ;; *) fail "stderr does not name the hold" ;; esac
case "$ERR" in *"ALERT EXP-1 code-review 25 needs-human could not be set (exit 1)"*) ;; *) fail "no alert for the unwritten label" ;; esac
rm -rf "$HOLD"
real none 'mark_needs_human EXP-1 merge 18'
case "$ERR" in *"ALERT EXP-1 merge 18 needs-human could not be set (exit 1)"*) ;; *) fail "the alert does not name the stage's own exit code" ;; esac
rm -rf "$HOLD"

real broken 'mark_needs_human EXP-1 qa; echo CONTINUED'
[ "$RC" = 27 ] || fail "Linear unusable must end with 27"
case "$OUT" in *CONTINUED*) fail "the stage carried on after 27" ;; esac
grep -q "exit=27" "$HOLD/EXP-1" 2>/dev/null || fail "Linear unusable left no hold"
rm -rf "$HOLD"

for evil in "../../EVIL-1" "/../../EVIL-2"; do
  real none "mark_needs_human '$evil' qa"
  [ "$RC" = 1 ] || fail "a reference that is not an identifier must still fail ($evil)"
  [ -z "$(find "$SB" -name 'EVIL-*' 2>/dev/null)" ] || fail "a non-identifier became a file ($evil)"
  case "$ERR" in *"could not be held locally"*) ;; *) fail "stderr does not say the ticket is not held ($evil)" ;; esac
done

hold EXP-1
real ok 'BUREAU_DRY_RUN=1 mark_needs_human EXP-1 qa'
[ "$RC" = 0 ] && [ -f "$HOLD/EXP-1" ] || fail "a dry run touched the hold"
rm -rf "$HOLD"
echo "PASS mark_needs_human: written → 0 and clears; failed → 1, held, alerted; 27 → held and 27; no path escape; dry run leaves holds alone"

# --- the picker ---------------------------------------------------------------------
real ok 'pipeline_pick_next code-review-pipeline.sh'
[ "$RC" = 0 ] && [ "$OUT" = EXP-1 ] || fail "without holds the picker takes EXP-1"
[ "$CALLS" = "list " ] || fail "without holds the picker must make exactly one Linear call, made: $CALLS"

hold EXP-1
real none 'pipeline_pick_next code-review-pipeline.sh'
[ "$RC" = 0 ] && [ "$OUT" = EXP-2 ] || fail "a held ticket was picked again"
[ -f "$HOLD/EXP-1" ] || fail "a label that still fails released the hold"
case "$ERR" in *"not written yet: EXP-1"*) ;; *) fail "the skip is not logged" ;; esac

real ok 'pipeline_pick_next code-review-pipeline.sh'
[ "$RC" = 0 ] && [ "$OUT" = EXP-2 ] || fail "after the label was written EXP-1 must stay out (by its label)"
[ ! -e "$HOLD/EXP-1" ] || fail "the hold outlived the written label"
case "$CALLS" in *add*list*) ;; *) fail "the label was not retried before the list: $CALLS" ;; esac

# One ticket must not stop a queue: only the label mutation fails (GraphQL errors → 27), the
# pick itself works. Every stage still picks, the held ticket stays held and skipped.
hold EXP-1
for stage in spec-pipeline.sh implement-pipeline.sh code-review-pipeline.sh; do
  real adderr "pipeline_pick_next $stage"
  [ "$RC" = 0 ] || fail "$stage: a label that Linear refuses stopped the pick"
  [ "$OUT" = EXP-2 ] || fail "$stage: expected EXP-2 while EXP-1 is held"
  [ -f "$HOLD/EXP-1" ] || fail "$stage: a refused label released the hold"
done
# After a 27 the remaining holds are not tried in this pick (one retry ladder, not one per ticket).
hold EXP-3
real adderr 'pipeline_pick_next code-review-pipeline.sh'
[ "$RC" = 0 ] && [ "$OUT" = EXP-2 ] || fail "two held tickets: expected EXP-2"
[ "$(grep -c '^add$' "$SB/linear.log")" = 1 ] || fail "after a 27 the next hold was tried in the same pick: $CALLS"
[ -f "$HOLD/EXP-1" ] && [ -f "$HOLD/EXP-3" ] || fail "an untried hold was released"
rm -f "$HOLD/EXP-3"
# A plain failure (no such label) keeps trying the other holds.
hold EXP-3
real none 'pipeline_pick_next code-review-pipeline.sh'
[ "$(grep -c '^lookup$' "$SB/linear.log")" = 2 ] || fail "a plain label failure stopped the other retries: $CALLS"
rm -f "$HOLD/EXP-3"
# Linear unusable for the pick itself: the pick's own read ends with 27, the hold stays.
real down 'pipeline_pick_next code-review-pipeline.sh'
[ "$RC" = 27 ] && [ -z "$OUT" ] || fail "Linear unusable for the pick must end it with 27"
[ -f "$HOLD/EXP-1" ] || fail "Linear unusable released the hold"
# The caller's own skip list survives the hold.
real none 'pipeline_pick_next code-review-pipeline.sh EXP-2'
[ "$RC" = 0 ] && [ -z "$OUT" ] || fail "hold EXP-1 plus the caller's skip EXP-2 must leave nothing to pick"

real ok 'BUREAU_DRY_RUN=1 pipeline_pick_next code-review-pipeline.sh'
[ "$RC" = 0 ] && [ "$OUT" = EXP-2 ] && [ -f "$HOLD/EXP-1" ] || fail "a dry run must honour the hold without writing"
case "$CALLS" in *add*) fail "a dry run wrote the label" ;; esac

# The holds and the pause marker belong to the repo of .bureau.json, not to the current directory.
real none 'cd / && pipeline_pick_next code-review-pipeline.sh'
[ "$RC" = 0 ] && [ "$OUT" = EXP-2 ] || fail "run from /: the hold of the config's repo was ignored"
real none 'cd / && mark_needs_human EXP-4 qa'
[ -f "$HOLD/EXP-4" ] || fail "run from /: the hold was not written into the config's repo"
rm -f "$HOLD/EXP-4"
mkdir -p "$SB/.git/bureau"; touch "$SB/.git/bureau/paused"
real ok 'cd / && bureau_is_paused && echo PAUSED'
[ "$OUT" = PAUSED ] || fail "run from /: the pause marker of the config's repo was not seen"
rm -f "$SB/.git/bureau/paused"
real ok 'bureau_is_paused && echo PAUSED'
[ -z "$OUT" ] || fail "no pause marker, but paused"

real none 'pipeline_pick_next code-review-pipeline.sh' "$SB/old/bureau-config.sh"
[ "$RC" = 0 ] && [ "$OUT" = EXP-1 ] || fail "negative control: the old picker should take the held ticket again"
rm -rf "$HOLD"
echo "PASS the picker skips a held ticket, retries its label and releases it once written; a refused label never stops a pick, an outage does; holds follow the config's repo; negative control re-picks it"

# --- the stage arms ------------------------------------------------------------------
sandbox_init "EXP-300" "test-branch"
AHOLD="$SANDBOX/.git/bureau/needs-human-held/EXP-300"
arm() {  # $1 = code block, $2 = label rc; sets ARM_OUT ARM_RC
  rm -f "$AHOLD"; : > "$SANDBOX/calls.log"
  set +e
  ARM_OUT=$(cd "$SANDBOX" && BUREAU_STUB_ADD_LABEL_RC="$2" bash -c "
    set -euo pipefail
    source '$SCRIPTS_DIR/bureau-config.sh'
    merge_origin_main_or_abort() { return 1; }
    ISSUE=EXP-300 BRANCH=test-branch STATUS=NEEDS_HUMAN DESIGN_STATUS=NEEDS_HUMAN SUMMARY=s
    QA_LOG_PATH=l QA_ESCALATION_REASON=r DESIGN_SUMMARY=d PR_NUMBER=7 MERGED_REVIEW=m REVIEW_CYCLE_COUNT=1
    $1
    echo ARM-CONTINUED" "$SCRIPTS_DIR/arm.sh" 2>&1)
  ARM_RC=$?
  set -e
}
cut() {  # $1 = file content, $2 = start regex, $3 = end regex (awk)
  printf '%s\n' "$1" | awk -v s="$2" -v e="$3" '$0 ~ s { f = 1 } f { print } f && $0 ~ e { exit }'
}
expect() {  # $1 = label, $2 = rc wanted, $3 = hold wanted (yes|no)
  [ "$ARM_RC" = "$2" ] || fail "$1: exit $ARM_RC, wanted $2: $ARM_OUT"
  if [ "$3" = yes ]; then [ -f "$AHOLD" ] || fail "$1: no hold"; else [ ! -e "$AHOLD" ] || fail "$1: unexpected hold"; fi
}

REBASE=$(cut "$(cat "$SCRIPTS/rebase-pipeline.sh")" 'Rebase produced conflicts — aborting' '^fi$' | sed '$d')
case "$REBASE" in *needs[-_]human*) ;; *) fail "the rebase conflict arm is not where it was" ;; esac
arm "$REBASE" 0;  expect "rebase conflict, label written" 0 no
arm "$REBASE" 1;  expect "rebase conflict, label failed" 25 yes
arm "$REBASE" 27; expect "rebase conflict, Linear unusable" 27 yes
OLD_REBASE='  post_comment "$ISSUE" "Rebase produced conflicts. Needs human resolution."
  add_issue_label "$ISSUE" "needs-human" \
    || echo "  WARN: failed to add needs-human label to $ISSUE; will retry on next tick" >&2
  exit 0'
arm "$OLD_REBASE" 1;  [ "$ARM_RC" = 0 ] || fail "negative control: old rebase arm with a failed label ended $ARM_RC, expected 0"
arm "$OLD_REBASE" 27; [ "$ARM_RC" = 0 ] || fail "negative control: old rebase arm swallowed 27 → expected 0, got $ARM_RC"

LEASE=$(cut "$(cat "$SCRIPTS/rebase-pipeline.sh")" 'Force-push rejected' '^    exit 19$')
case "$LEASE" in *needs[-_]human*) ;; *) fail "the rebase lease arm is not where it was" ;; esac
arm "$LEASE" 1;  expect "rebase lease rejected, label failed" 19 yes
arm "$LEASE" 27; expect "rebase lease rejected, Linear unusable" 27 yes
grep -q '^alert_telegram	EXP-300	rebase	27' "$SANDBOX/calls.log" && fail "a 27 alerted as a label failure"
arm "$LEASE" 1; grep -q '^alert_telegram	EXP-300	rebase	19	' "$SANDBOX/calls.log" || fail "the lease arm's alert does not name 19"

MERGE=$(cut "$(cat "$SCRIPTS/merge-pipeline.sh")" 'Merge call failed\.' '^  exit 18$')
case "$MERGE" in *needs[-_]human*) ;; *) fail "the merge failure arm is not where it was" ;; esac
arm "$MERGE" 1;  expect "merge failure, label failed" 18 yes
arm "$MERGE" 27; expect "merge failure, Linear unusable" 27 yes
OLD_MERGE='  add_issue_label "$ISSUE" "needs-human" \
    || echo "  WARN: failed to add needs-human label to $ISSUE; will retry on next tick" >&2
  exit 18'
arm "$OLD_MERGE" 27; [ "$ARM_RC" = 18 ] || fail "negative control: old merge arm should swallow 27 into 18, got $ARM_RC"

CR=$(cut "$(cat "$SCRIPTS/code-review-pipeline.sh")" 'Blocked — needs human review' '^    ;;$')
case "$CR" in *needs[-_]human*) ;; *) fail "the code-review BLOCK arm is not where it was" ;; esac
arm "case BLOCK in
  BLOCK|*)
$CR
esac" 0; expect "code-review BLOCK, label written" 0 no
grep -q '^log_escalation	EXP-300	code-review' "$SANDBOX/calls.log" || fail "code-review: a written label no longer logs the escalation"
arm "case BLOCK in
  BLOCK|*)
$CR
esac" 1; expect "code-review BLOCK, label failed (the stage's 25 comes from the verdict)" 0 yes
grep -q '^log_escalation' "$SANDBOX/calls.log" && fail "code-review: escalation logged although the label failed"
grep -q '^post_comment	EXP-300' "$SANDBOX/calls.log" || fail "code-review: the BLOCK comment must still go out"

IMPL=$(cut "$(cat "$SCRIPTS/implement-pipeline.sh")" '^if ! merge_origin_main_or_abort' '^fi$')
case "$IMPL" in *needs[-_]human*) ;; *) fail "the implement conflict arm is not where it was" ;; esac
arm "$IMPL" 1;  expect "implement conflict, label failed" 17 yes
arm "$IMPL" 27; expect "implement conflict, Linear unusable" 27 yes

tail_from() {  # $1 = script, $2 = routing case line: from the flag's init (or the case) to the end
  local start
  start=$(awk '/^NEEDS_HUMAN_UNMARKED=0$/ { n = NR } END { print n }' "$1")
  [ -n "$start" ] || start=$(awk -v c="$2" '$0 == c { n = NR } END { print n }' "$1")
  sed -n "${start},\$p" "$1"
}
QA=$(tail_from "$SCRIPTS/qa-pipeline.sh" 'case "$STATUS" in')
case "$QA" in *needs[-_]human*) ;; *) fail "the QA routing block is not where it was" ;; esac
arm "$QA" 0; expect "QA needs-human, label written" 0 no
grep -q '^log_escalation	EXP-300	qa' "$SANDBOX/calls.log" || fail "QA: a written label no longer logs the escalation"
arm "$QA" 1; expect "QA needs-human, label failed" 25 yes
grep -q '^log_escalation' "$SANDBOX/calls.log" && fail "QA: escalation logged although the label failed"
grep -q '^post_comment	EXP-300' "$SANDBOX/calls.log" || fail "QA: the comment must still go out"

UX=$(tail_from "$SCRIPTS/ux-pipeline.sh" 'case "$DESIGN_STATUS" in')
case "$UX" in *needs[-_]human*) ;; *) fail "the UX routing block is not where it was" ;; esac
arm "$UX" 0; expect "UX needs-human, label written" 0 no
arm "$UX" 1; expect "UX needs-human, label failed" 25 yes
arm "$UX" 27; expect "UX needs-human, Linear unusable" 27 yes
teardown
echo "PASS the stage arms hold the ticket and end non-zero when the label fails; 27 halts; negative controls end 0 / swallow 27"

# --- QA keeps no temp dir when it ends 25 for an unwritten label --------------------------
QC=$(sed -n '/^QA_TMP=\$(mktemp -d)$/,/^trap _qa_cleanup EXIT$/p' "$SCRIPTS/qa-pipeline.sh")
case "$QC" in *_qa_cleanup*) ;; *) fail "the QA cleanup trap is not where it was" ;; esac
qa_tmp() {  # $1 = exit code, $2 = flag; prints the temp dir if it survived
  /bin/bash -c "$QC
echo \"\$QA_TMP\" > '$SB/qa_tmp'; NEEDS_HUMAN_UNMARKED=$2; exit $1" || true
  d=$(cat "$SB/qa_tmp"); [ -d "$d" ] && { echo "$d"; rm -rf "$d"; } || true
}
[ -z "$(qa_tmp 25 1)" ] || fail "QA left its temp dir after ending 25 for an unwritten label"
[ -n "$(qa_tmp 1 0)" ] || fail "negative control: a real QA failure must keep its temp dir"
echo "PASS QA removes its temp dir when it ends 25 for an unwritten label, and keeps it on a real failure"

# --- queue loop: a pick that fails with 27 is reported, not read as an empty queue ----------
Q="$SB/queue"; mkdir -p "$Q/scripts"
printf '#!/bin/bash\nexit 0\n' > "$Q/scripts/bureau-worker.sh"
{
  sed -n '/^exit_class() {/,/^}/p' "$SCRIPTS/bureau-config.sh"
  sed -n '/^run_script() {/,/^}/p' "$SCRIPTS/queue-loop.sh"
} > "$Q/queue.sh"
grep -q '^run_script() {' "$Q/queue.sh" || fail "run_script not found in queue-loop.sh"
queue_pick() {  # $1 = picker exit code, $2 = file holding run_script; sets QRC, QALERTS, QLOG
  rm -f "$Q/alerts" "$Q/log"
  set +e
  Q="$Q" PICK_RC="$1" BUREAU_EXIT_LINEAR_UNUSABLE=27 /bin/bash -c '
    source "$0"
    REPO_DIR="$Q"; LOG_FILE="$Q/log"; MODE=all
    preselect_issue() { return "$PICK_RC"; }
    bureau_merge_is_manual() { return 1; }
    stop_before_merge_was_asked() { return 1; }
    alert_telegram() { echo "$*" >> "$Q/alerts"; }
    run_script code-review-pipeline.sh "Code Review" "$Q"' "$2" >/dev/null 2>&1
  QRC=$?
  set -e
  QALERTS=$(cat "$Q/alerts" 2>/dev/null || true); QLOG=$(cat "$Q/log" 2>/dev/null || true)
}
queue_pick 27 "$Q/queue.sh"
[ "$QRC" = 27 ] || fail "queue: a pick that failed with 27 returned $QRC"
case "$QALERTS" in *"none code-review-pipeline.sh 27 "*linear-unusable*) ;; *) fail "queue: no alert for the failed pick: $QALERTS" ;; esac
case "$QLOG" in *"pick failed: Linear stayed unusable (exit 27)"*) ;; *) fail "queue: the failed pick is not logged" ;; esac
queue_pick 1 "$Q/queue.sh"
[ "$QRC" = 2 ] && [ -z "$QALERTS" ] || fail "queue: any other pick failure stays an empty queue without an alert (rc=$QRC)"
case "$QLOG" in *"pick failed (exit 1); treated as an empty queue"*) ;; *) fail "queue: the other failure is not logged" ;; esac
# Negative control: the old line reads the 27 as an empty queue, silently.
sed 's/^  picked=\$(preselect_issue "\$script" 2>\/dev\/null) || pick_rc=\$?$/  picked=$(preselect_issue "$script" 2>\/dev\/null || true)/' "$Q/queue.sh" > "$Q/old.sh"
cmp -s "$Q/queue.sh" "$Q/old.sh" && fail "negative control: the pick line was not found to revert"
queue_pick 27 "$Q/old.sh"
[ "$QRC" = 2 ] && [ -z "$QALERTS" ] || fail "negative control: the old line should read 27 as an empty queue (rc=$QRC)"
echo "PASS the queue reports a pick that failed with 27 and alerts; other failures stay an empty queue; negative control reads 27 as empty"
