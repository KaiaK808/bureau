#!/bin/bash
# When a Linear read fails for good (exit 27), the stages stop instead of deciding on nothing.
#
# The helpers' own behaviour is in tests/test_linear_retry.sh; here the stub config returns 27
# from a read, as the real one does once Linear stays unusable, and the REAL stages run:
#   - implement: the per-iteration review-context read fails → the stage ends with 27 before
#     any provider round runs without the reviewer's requested fixes
#   - implement: the needs-human label fails with 27 → the stage ends with 27; any other
#     failure holds the ticket locally and ends with 25 (tests/test_needs_human_hold.sh)
#   - spec: the issue read fails → the rollback names linear-unusable
#   - qa and code-review: their needs-human arms, cut out of the real scripts, end with 27
# The halt helper and its exit code come from the real config (tests/lib/harness.sh).
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

fail() { echo "FAIL $*" >&2; exit 1; }

# --- implement: review context ------------------------------------------------------
sandbox_init "EXP-110" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
export FAKE_CLAUDE_LOG="$SANDBOX/claude.log"
export BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
export BUREAU_STUB_COMMENTS_RC=27 BUREAU_STUB_COMMENTS_RC_FROM=2   # branch lookup works, the review read does not
run_implement_pipeline
unset BUREAU_STUB_COMMENTS_RC BUREAU_STUB_COMMENTS_RC_FROM
assert_eq 27 "$LAST_RC" "implement exit when the review context cannot be read"
[ ! -s "$SANDBOX/claude.log" ] || fail "a provider round ran without the review context"
assert_calls_exclude '^move_issue' "no state move after the failed read"
echo "PASS implement stops with 27 before a round runs without the review feedback"
teardown

# --- implement: needs-human label ---------------------------------------------------
sandbox_init "EXP-111" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_no_progress.txt"
export BUREAU_STUB_ADD_LABEL_RC=27
run_implement_pipeline
assert_eq 27 "$LAST_RC" "implement exit when needs-human cannot be set"
assert_calls_exclude '^log_escalation' "no escalation logged into the void"
assert_match 'cannot decide without this answer' "$LAST_STDERR" "halt names the reason"
teardown

sandbox_init "EXP-112" "test-branch"
export BUREAU_STUB_ADD_LABEL_RC=1
run_implement_pipeline
unset BUREAU_STUB_ADD_LABEL_RC FAKE_CLAUDE_LOG
assert_eq 25 "$LAST_RC" "any other label failure ends with 25, not success"
assert_match "could not add 'needs-human' to EXP-112 \(exit 1\) — held in " "$LAST_STDERR" "the hold is named"
echo "PASS a label write that fails with 27 halts the stage; any other failure holds the ticket and ends with 25"
teardown

# --- spec: rollback names the class ---------------------------------------------------
sandbox_init "EXP-113" "test-branch"
export BUREAU_STUB_DETAIL_RC=27 BUREAU_STUB_ISSUE_STATE=Triage
run_pipeline spec-pipeline.sh "EXP-113"
unset BUREAU_STUB_DETAIL_RC BUREAU_STUB_ISSUE_STATE
assert_eq 27 "$LAST_RC" "spec exit"
assert_match 'exit 27 / linear-unusable' "$LAST_STDERR" "rollback names linear-unusable"
assert_calls_include 'post_comment.*linear-unusable' "rollback comment names the class"
echo "PASS spec rolls back with the class linear-unusable"
teardown

# --- qa and code-review: the needs-human arms, cut from the real scripts -------------
sandbox_init "EXP-114" "test-branch"
SCRIPTS="$REPO_ROOT/templates/scripts"
QA_ARM=$(awk '/^case "\$STATUS" in$/ { f = 1 } f { print } f && /^esac$/ { exit }' "$SCRIPTS/qa-pipeline.sh")
CR_ARM=$(awk '/^  BLOCK\|\*\)$/ { f = 1 } f { print } f && /^    ;;$/ { exit }' "$SCRIPTS/code-review-pipeline.sh")
case "$QA_ARM" in *'mark_needs_human'*) ;; *) fail "the QA routing block is not where it was" ;; esac
case "$CR_ARM" in *'mark_needs_human'*) ;; *) fail "the code-review BLOCK arm is not where it was" ;; esac
arm() {  # $1 = code block; runs it with the stub config and a label write that fails with 27
  set +e
  ARM_OUT=$(cd "$SANDBOX" && BUREAU_STUB_ADD_LABEL_RC=27 bash -c "
    set -euo pipefail
    source '$SCRIPTS_DIR/bureau-config.sh'
    ISSUE=EXP-114 BRANCH=test-branch STATUS=NEEDS_HUMAN VERDICT=BLOCK SUMMARY=s QA_LOG_PATH=l
    QA_ESCALATION_REASON=r REVIEW_CYCLE_COUNT=1 PR_NUMBER=7 MERGED_REVIEW=m
    $1
    echo ARM-CONTINUED" "$SCRIPTS_DIR/arm.sh" 2>&1)   # \$0 in scripts/: the stub finds its siblings by it
  ARM_RC=$?
  set -e
}
arm "$QA_ARM"
[ "$ARM_RC" = 27 ] || fail "QA needs-human arm: exit $ARM_RC, wanted 27: $ARM_OUT"
case "$ARM_OUT" in *ARM-CONTINUED*) fail "QA carried on after the failed label write" ;; esac
arm "case BLOCK in
$CR_ARM
esac"
[ "$ARM_RC" = 27 ] || fail "code-review BLOCK arm: exit $ARM_RC, wanted 27"
case "$ARM_OUT" in *ARM-CONTINUED*) fail "code review carried on after the failed label write" ;; esac
echo "PASS the QA and code-review needs-human arms stop with 27 when the label cannot be written"
