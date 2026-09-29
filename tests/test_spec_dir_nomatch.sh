#!/bin/bash
# Implement, spec review and UX when the branch matches no spec directory, and when it
# matches one that lacks tasks.md.
#   - no directory matches: exit 13, back to Spec (implement, spec review) or Spec Review
#     (UX), needs-human set, the comment names the branch and the specs directory. Before,
#     the ticket went back without the label, and nothing but a human or a Linear
#     automation moved it on again (the queue never picks a ticket in Spec).
#   - a directory matches but has no tasks.md (implement, spec review): exit 13, back to
#     Spec without the label, as before — /speckit-tasks can repair that.
# Runs the REAL stages in the harness sandbox (stub Linear and gh, fake agent) with the
# real matcher and the real needs-human hold helpers.
set -uo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP

failed=0
row_errors=""
err() { row_errors="${row_errors}    - $*"$'\n'; }
finish_row() {  # <label>
  if [ -z "$row_errors" ]; then
    echo "PASS $1"
  else
    failed=$((failed + 1))
    echo "FAIL $1" >&2
    printf '%s' "$row_errors" >&2
    printf '%s\n' "${LAST_STDOUT:-}" | tail -6 | sed 's/^/      out: /' >&2
  fi
  row_errors=""
  unset BUREAU_STUB_ADD_LABEL_RC
  teardown
}

# setup <Linear state> <spec layout>
#   nomatch — the only spec directory is 002-unrelated-work (shares no word with the branch)
#   notasks — 001-test-branch exists with spec.md but without tasks.md
setup() {
  sandbox_init EXP-920 001-test-branch
  case "$2" in
    nomatch) mv "$SANDBOX/specs/001-test-branch" "$SANDBOX/specs/002-unrelated-work" ;;
    notasks) rm "$SANDBOX/specs/001-test-branch/tasks.md"; printf '# spec\n' > "$SANDBOX/specs/001-test-branch/spec.md" ;;
  esac
  git -C "$SANDBOX" add -A specs
  git -C "$SANDBOX" commit -q -m "fixture: $2"
  git -C "$SANDBOX" push -q origin 001-test-branch
  export FAKE_CLAUDE_PROMPT_LOG="$SANDBOX/prompts.log"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_filler.txt"
  export BUREAU_STUB_ISSUE_STATE="$1"
  unset FAKE_CLAUDE_COMMIT_ON_ITERS
}

last_comment() { grep '^post_comment' "$SANDBOX/calls.log" | tail -1; }
common() {  # <state the ticket goes back to>
  [ "$LAST_RC" = 13 ] || err "expected exit 13, got $LAST_RC"
  grep -qE "^move_issue	EXP-920	$1\$" "$SANDBOX/calls.log" || err "no move back to $1"
  [ ! -s "$SANDBOX/prompts.log" ] || err "the agent was called"
}
# expect_nomatch <state the ticket goes back to>
expect_nomatch() {
  common "$1"
  grep -qE '^add_issue_label	EXP-920	needs-human$' "$SANDBOX/calls.log" || err "needs-human not set"
  case "$(last_comment)" in
    *'no spec directory under `'*'/specs/` matches branch `001-test-branch`'*) : ;;
    *) err "comment does not name the specs directory and the branch: $(last_comment)" ;;
  esac
  case "$(last_comment)" in *'remove `needs-human`'*) : ;; *) err "comment does not say to remove needs-human" ;; esac
  case "$(last_comment)" in *'tasks.md'*) err "comment claims a tasks.md problem: $(last_comment)" ;; esac
}
# expect_notasks — a matched directory without tasks.md: back to Spec, no label.
expect_notasks() {
  common state-spec
  ! grep -q '^add_issue_label' "$SANDBOX/calls.log" || err "a label was written: $(grep '^add_issue_label' "$SANDBOX/calls.log")"
  case "$(last_comment)" in *'tasks.md'*) : ;; *) err "comment does not name tasks.md: $(last_comment)" ;; esac
  case "$(last_comment)" in *'no spec directory'*) err "comment claims no directory matched: $(last_comment)" ;; esac
}

setup Build nomatch
run_pipeline implement-pipeline.sh EXP-920; set +e
expect_nomatch state-spec
finish_row "implement: no directory matches → 13, needs-human, back to Spec"

setup Build notasks
run_pipeline implement-pipeline.sh EXP-920; set +e
expect_notasks
finish_row "implement: matched directory without tasks.md → 13, no label, back to Spec"

setup 'Spec Review' nomatch
run_pipeline spec-review-pipeline.sh EXP-920; set +e
expect_nomatch state-spec
finish_row "spec review: no directory matches → 13, needs-human, back to Spec"

setup 'Spec Review' notasks
run_pipeline spec-review-pipeline.sh EXP-920; set +e
expect_notasks
finish_row "spec review: matched directory without tasks.md → 13, no label, back to Spec"

setup Design nomatch
run_pipeline ux-pipeline.sh EXP-920; set +e
expect_nomatch state-spec-review
finish_row "ux: no directory matches → 13, needs-human, back to Spec Review"

# The label cannot be written: the ticket is held locally (real hold helpers), the stage
# still ends with 13, and the alert says so.
setup Build nomatch
export BUREAU_STUB_ADD_LABEL_RC=1
run_pipeline implement-pipeline.sh EXP-920; set +e
[ "$LAST_RC" = 13 ] || err "expected exit 13, got $LAST_RC"
[ -f "$SANDBOX/.git/bureau/needs-human-held/EXP-920" ] || err "no local hold written"
grep -q $'^alert_telegram\tEXP-920\timplement\t13\tneeds-human could not be set' "$SANDBOX/calls.log" || err "no alert about the unwritten label"
finish_row "implement: no match and an unwritable label → 13, held locally, alert"

if [ "$failed" -gt 0 ]; then
  echo "FAILED rows: $failed" >&2
  exit 1
fi
echo "OK test_spec_dir_nomatch"
