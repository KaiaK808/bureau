#!/bin/bash
# spec-pipeline.sh and .specify/feature.json after the speckit-specify call.
#
#   - missing or malformed: exit 11 with the "absent or invalid" comment, back to Triage,
#     no needs-human (a re-run may succeed). Before, jq's own status escaped (2 for a
#     missing file, 5 for invalid JSON), and 2 reads as "queue empty" to both drivers.
#   - stale: exit 11, back to Triage AND needs-human (a re-run reads the same file).
#     Stale means all three: the file is byte for byte what it was before the call, the
#     directory it names existed before the call, and that directory is not the ticket's
#     own (bureau_spec_dir_for_branch of the branch checked out before the call, never
#     main, or of the ticket's newest bureau-branch marker; Linear's generated branch name
#     does not count). Before, such a file was accepted and plan and tasks ran on another
#     ticket's spec directory.
#   - anything else proceeds as before.
# Runs the REAL spec-pipeline.sh in the harness sandbox (stub Linear, a fake agent that
# writes, removes, corrupts or leaves the file on the specify call: lib/pr4-fake-specify.sh).
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
    printf '%s\n' "${LAST_STDOUT:-}" | tail -8 | sed 's/^/      out: /' >&2
    printf '%s\n' "${LAST_STDERR:-}" | tail -4 | sed 's/^/      err: /' >&2
  fi
  row_errors=""
  unset PR4_SPECIFY_ACTION PR4_SPECIFY_DIR PR4_SPECIFY_RAW BUREAU_STUB_REVIEW_COMMENTS BUREAU_STUB_REVIEW_COMMENTS_RC
  teardown
}

# setup <mode> [<feature.json content before the call>]
#   mode detached — HEAD detached at the branch tip, as a disposable spec worker starts
#        on-branch — the ticket's branch 001-test-branch checked out
#        on-main  — main checked out
# Spec directories: 001-test-branch (from the harness) and 003-older-feature, committed.
# The file before the call is committed too (installations track it).
setup() {
  sandbox_init EXP-910 001-test-branch
  mkdir -p "$SANDBOX/specs/003-older-feature"
  printf '# older spec\n' > "$SANDBOX/specs/003-older-feature/spec.md"
  printf '# older tasks\n- [ ] T001 older task\n' > "$SANDBOX/specs/003-older-feature/tasks.md"
  if [ -n "${2:-}" ]; then
    mkdir -p "$SANDBOX/.specify"
    printf '%s\n' "$2" > "$SANDBOX/.specify/feature.json"
  fi
  git -C "$SANDBOX" add -A specs
  [ ! -d "$SANDBOX/.specify" ] || git -C "$SANDBOX" add -A .specify
  git -C "$SANDBOX" commit -q -m 'fixture specs'
  git -C "$SANDBOX" push -q origin 001-test-branch
  case "$1" in
    detached) git -C "$SANDBOX" checkout -q --detach ;;
    on-branch) : ;;
    on-main) git -C "$SANDBOX" checkout -q main && git -C "$SANDBOX" merge -q --ff-only 001-test-branch ;;
  esac
  export FAKE_CLAUDE_BIN="$LIB_DIR/pr4-fake-specify.sh"
  chmod +x "$FAKE_CLAUDE_BIN"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_filler.txt"
  export FAKE_CLAUDE_PROMPT_LOG="$SANDBOX/prompts.log"
  export BUREAU_STUB_ISSUE_STATE=Triage
  unset FAKE_CLAUDE_COMMIT_ON_ITERS
}

agent_calls() { cat "$SANDBOX/fake_claude_counter" 2>/dev/null || echo 0; }
last_comment() { grep '^post_comment' "$SANDBOX/calls.log" | tail -1; }

# expect_abort <needs-human: yes|no> <comment fragment>
expect_abort() {
  [ "$LAST_RC" = 11 ] || err "expected exit 11, got $LAST_RC"
  [ "$(agent_calls)" = 1 ] || err "expected no agent call after specify, got $(agent_calls) calls in all"
  grep -qE $'^move_issue\tEXP-910\tstate-triage$' "$SANDBOX/calls.log" || err "no move back to Triage"
  grep -qE $'^alert_telegram\tEXP-910\tspec-pipeline\t11\t' "$SANDBOX/calls.log" || err "no alert with 11"
  if [ "$1" = yes ]; then
    grep -qE $'^add_issue_label\tEXP-910\tneeds-human$' "$SANDBOX/calls.log" || err "needs-human not set"
  else
    ! grep -q '^add_issue_label' "$SANDBOX/calls.log" || err "a label was written: $(grep '^add_issue_label' "$SANDBOX/calls.log")"
  fi
  case "$(last_comment)" in *"$2"*) : ;; *) err "last comment lacks '$2': $(last_comment)" ;; esac
  if grep -q '^git checkout -b\|Creating branch' <<< "$LAST_STDOUT"; then err "the stage went on to create a branch"; fi
}
# expect_proceed <spec dir the plan works on>
expect_proceed() {
  [ "$LAST_RC" = 0 ] || err "expected exit 0, got $LAST_RC"
  grep -qE $'^move_issue\tEXP-910\tstate-spec-review$' "$SANDBOX/calls.log" || err "not moved to Spec Review"
  ! grep -q '^add_issue_label' "$SANDBOX/calls.log" || err "a label was written"
  grep -qF "Work on the spec in $1" "$SANDBOX/prompts.log" || err "plan/tasks were not told to work on $1"
}

OLDER='{"feature_directory":"specs/003-older-feature"}'
MARKER_OWN_004='[{"body":"<!-- bureau-branch: 004-own-feature -->\n**Spec Artifacts — EXP-910**","createdAt":"2026-01-02T00:00:00Z"}]'
MARKER_OTHER='[{"body":"<!-- bureau-branch: 001-test-branch -->\n**Spec Artifacts — EXP-910**","createdAt":"2026-01-02T00:00:00Z"}]'

# F1 — no file before, specify writes none.
setup detached
export PR4_SPECIFY_ACTION=leave
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort no 'absent or invalid'
finish_row "F1 missing feature.json → 11, Triage, no needs-human"

# F2 — specify removes the file.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=remove
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort no 'absent or invalid'
finish_row "F2 feature.json removed by specify → 11, no needs-human"

# F3 — not JSON.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=raw PR4_SPECIFY_RAW='{"feature_directory": '
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort no 'absent or invalid'
finish_row "F3 malformed feature.json → 11, no needs-human"

# F4 — a valid object followed by garbage: jq prints the directory, then fails.
setup detached
export PR4_SPECIFY_ACTION=raw PR4_SPECIFY_RAW='{"feature_directory":"specs/003-older-feature"} }'
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort no 'absent or invalid'
finish_row "F4 a directory printed before a JSON error does not count → 11"

# F5 — valid JSON without the key.
setup detached
export PR4_SPECIFY_ACTION=raw PR4_SPECIFY_RAW='{}'
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort no 'absent or invalid'
finish_row "F5 feature.json without feature_directory → 11"

# S1 — stale: specify creates its directory but leaves the tracked file naming another
# ticket's older directory.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=mkdir PR4_SPECIFY_DIR=specs/004-new-feature
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/003-older-feature`'
case "$(last_comment)" in *'remove `needs-human`'*) : ;; *) err "comment does not say to remove needs-human" ;; esac
finish_row "S1 stale feature.json → 11, Triage, needs-human, no agent call after specify"

# S2 — stale although the ticket has a marker: the marker's directory is another one.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=leave BUREAU_STUB_REVIEW_COMMENTS="$MARKER_OTHER"
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/003-older-feature`'
finish_row "S2 a marker naming another directory does not make the file the ticket's"

# S3 — Linear's generated branch name is not the ticket's branch: it would resolve to the
# older directory, but only the marker counts.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=leave BUREAU_STUB_BRANCH=someone/exp-910-older-feature
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/003-older-feature`'
! grep -q '^get_issue_branch' "$SANDBOX/calls.log" || err "the stale check read the Linear fallback branch"
export BUREAU_STUB_BRANCH=001-test-branch
finish_row "S3 Linear's generated branch name does not make the file the ticket's"

# S4 — main checked out: main is never the ticket's branch, even when a directory's
# slug starts with "main".
setup on-main '{"feature_directory":"specs/003-main-menu"}'
mkdir -p "$SANDBOX/specs/003-main-menu"; printf '# menu\n' > "$SANDBOX/specs/003-main-menu/spec.md"
git -C "$SANDBOX" add -A specs; git -C "$SANDBOX" commit -q -m 'main menu spec'
export PR4_SPECIFY_ACTION=leave
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/003-main-menu`'
finish_row "S4 on main, a directory fitting the name main is not the ticket's"

# S5 — a Linear read that fails during the marker lookup ends the stage with its code
# (27 through the recovery trap); it never counts as "no marker".
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=leave BUREAU_STUB_REVIEW_COMMENTS_RC=27
run_pipeline spec-pipeline.sh EXP-910; set +e
[ "$LAST_RC" = 27 ] || err "expected exit 27, got $LAST_RC"
[ "$(agent_calls)" = 1 ] || err "agent called after specify: $(agent_calls)"
! grep -q '^add_issue_label' "$SANDBOX/calls.log" || err "a label was written"
finish_row "S5 a failed marker read ends with its own code"

# P1 — a proper run: specify records its new directory.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=write PR4_SPECIFY_DIR=specs/004-new-feature
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_proceed specs/004-new-feature/
if grep -q 'specs/003-older-feature' "$SANDBOX/prompts.log"; then err "a prompt names the older directory"; fi
finish_row "P1 a file specify rewrote proceeds on its directory"

# P2 — unchanged file, but the directory is the ticket's own by its marker (a re-spec of
# a ticket whose directory is already on main).
setup detached '{"feature_directory":"specs/004-own-feature"}'
mkdir -p "$SANDBOX/specs/004-own-feature"; printf '# own\n' > "$SANDBOX/specs/004-own-feature/spec.md"
git -C "$SANDBOX" add -A specs; git -C "$SANDBOX" commit -q -m 'own spec'
export PR4_SPECIFY_ACTION=leave BUREAU_STUB_REVIEW_COMMENTS="$MARKER_OWN_004"
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_proceed specs/004-own-feature/
finish_row "P2 unchanged file naming the marker's directory proceeds"

# P3 — unchanged file, the ticket's branch checked out, no marker (a retry in a checkout
# on the ticket's branch).
setup on-branch '{"feature_directory":"specs/001-test-branch"}'
export PR4_SPECIFY_ACTION=leave
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_proceed specs/001-test-branch/
finish_row "P3 unchanged file naming the checked-out branch's directory proceeds"

# P4 — changed bytes naming a directory that existed: specify wrote the file in this run.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=raw PR4_SPECIFY_RAW='{"feature_directory": "specs/003-older-feature", "note": "rewritten"}'
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_proceed specs/003-older-feature/
finish_row "P4 a file specify changed is trusted"

# P5 — unchanged file naming a directory that did not exist before the call and that
# specify created.
setup detached '{"feature_directory":"specs/006-planned-feature"}'
export PR4_SPECIFY_ACTION=mkdir PR4_SPECIFY_DIR=specs/006-planned-feature
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_proceed specs/006-planned-feature/
finish_row "P5 a directory specify created counts, even through an unchanged file"

if [ "$failed" -gt 0 ]; then
  echo "FAILED rows: $failed" >&2
  exit 1
fi
echo "OK test_spec_feature_json"
