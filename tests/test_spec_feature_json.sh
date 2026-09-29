#!/bin/bash
# spec-pipeline.sh and .specify/feature.json after the speckit-specify call.
#
#   - missing or malformed: exit 11 with the "absent or invalid" comment, back to Triage,
#     no needs-human (a re-run may succeed). Before, jq's own status escaped (2 for a
#     missing file, 5 for invalid JSON), and 2 reads as "queue empty" to both drivers.
#   - stale: exit 11, back to Triage AND needs-human (a re-run reads the same file).
#     Stale means all three: the file holds byte for byte what it held before the call
#     (a stale value written back during the call counts too), the directory it names
#     existed before the call, and that directory is not the ticket's own: not the spec
#     directory named
#     exactly like the last segment of the branch checked out before the call or of the
#     ticket's newest bureau-branch marker (no fuzzy match; Linear's generated branch name
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
# Spec directories: 001-test-branch (from the harness) and 003-older-feature, committed,
# plus the directories named after the file content. The file before the call is committed
# too (installations track it).
setup() {
  local d
  sandbox_init EXP-910 001-test-branch
  mkdir -p "$SANDBOX/specs/003-older-feature"
  printf '# older spec\n' > "$SANDBOX/specs/003-older-feature/spec.md"
  printf '# older tasks\n- [ ] T001 older task\n' > "$SANDBOX/specs/003-older-feature/tasks.md"
  for d in "${@:3}"; do mkdir -p "$SANDBOX/specs/$d"; printf '# %s\n' "$d" > "$SANDBOX/specs/$d/spec.md"; done
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

# S4 — main checked out: a directory whose slug starts with "main" is not named like the
# branch main.
setup on-main '{"feature_directory":"specs/003-main-menu"}' 003-main-menu
export PR4_SPECIFY_ACTION=leave
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/003-main-menu`'
finish_row "S4 on main, a directory whose slug starts with main is not the ticket's"

# S5 — a Linear read that fails during the marker lookup ends the stage with its code
# (27 through the recovery trap); it never counts as "no marker".
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=leave BUREAU_STUB_REVIEW_COMMENTS_RC=27
run_pipeline spec-pipeline.sh EXP-910; set +e
[ "$LAST_RC" = 27 ] || err "expected exit 27, got $LAST_RC"
[ "$(agent_calls)" = 1 ] || err "agent called after specify: $(agent_calls)"
! grep -q '^add_issue_label' "$SANDBOX/calls.log" || err "a label was written"
finish_row "S5 a failed marker read ends with its own code"

# S6 — a re-spec: the worker starts on origin/main, where the ticket's own directory
# (003-auth-sso, on its unmerged spec branch) is missing. The marker would fit the sibling
# 002-auth by slug, but only a directory named exactly like the marker is the ticket's.
setup detached '{"feature_directory":"specs/002-auth"}' 002-auth
export PR4_SPECIFY_ACTION=leave BUREAU_STUB_REVIEW_COMMENTS='[{"body":"<!-- bureau-branch: 003-auth-sso -->\n**Spec Artifacts — EXP-910**","createdAt":"2026-01-02T00:00:00Z"}]'
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/002-auth`'
finish_row "S6 a marker that only fits a sibling by slug does not make it the ticket's"

# S7 — the checked-out branch fits the named directory by slug but is not its name.
setup on-branch '{"feature_directory":"specs/003-older-feature"}'
git -C "$SANDBOX" checkout -q -b 009-older-feature-v2
export PR4_SPECIFY_ACTION=leave
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/003-older-feature`'
finish_row "S7 a checked-out branch that only fits by slug does not make the file the ticket's"

# S8 — a directory named like the marker, but outside the specs directory.
setup detached '{"feature_directory":"docs/004-own-feature"}'
mkdir -p "$SANDBOX/docs/004-own-feature" "$SANDBOX/specs/004-own-feature"
printf '# elsewhere\n' > "$SANDBOX/docs/004-own-feature/spec.md"; printf '# own\n' > "$SANDBOX/specs/004-own-feature/spec.md"
git -C "$SANDBOX" add -A docs specs; git -C "$SANDBOX" commit -q -m 'same name outside specs'
export PR4_SPECIFY_ACTION=leave BUREAU_STUB_REVIEW_COMMENTS="$MARKER_OWN_004"
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `docs/004-own-feature`'
finish_row "S8 only the specs directory's entry of that name is the ticket's"

# P1 — a proper run: specify records its new directory.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=write PR4_SPECIFY_DIR=specs/004-new-feature
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_proceed specs/004-new-feature/
if grep -q 'specs/003-older-feature' "$SANDBOX/prompts.log"; then err "a prompt names the older directory"; fi
finish_row "P1 a file specify rewrote proceeds on its directory"

# P2 — unchanged file, but the directory is the ticket's own by its marker (a re-spec of
# a ticket whose directory is already on main).
setup detached '{"feature_directory":"specs/004-own-feature"}' 004-own-feature
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

# P6 — specify rewrites the file to another existing directory of the same length: the
# bytes differ, so the file is new (a size comparison would miss it).
setup detached "$OLDER" 004-older-feature
export PR4_SPECIFY_ACTION=raw PR4_SPECIFY_RAW='{"feature_directory":"specs/004-older-feature"}
'
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_proceed specs/004-older-feature/
finish_row "P6 a same-length rewrite to another directory is new content"

# S9 — specify writes the stale content back (an agent's unconditional rewrite): only the
# content counts, so this is still stale.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=rewrite
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/003-older-feature`'
finish_row "S9 the stale value written back is still stale"

# S10 — specify writes a new value, then `git checkout` restores main's stale file.
setup detached "$OLDER"
export PR4_SPECIFY_ACTION=restore PR4_SPECIFY_DIR=specs/004-new-feature
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/003-older-feature`'
finish_row "S10 a new value restored to the old file is still stale"

# S11 and P7 — the ticket's own directory is on main and named by main's file, but the
# ticket has no marker (a spec made by hand, or a lost marker), and specify re-records the
# same value. The name cannot tell it from a stale file, so the stage holds it (S11). The
# documented release: post the marker comment naming that directory, remove needs-human,
# and the next run proceeds on it (P7, same checkout).
setup detached '{"feature_directory":"specs/005-own-feature"}' 005-own-feature
export PR4_SPECIFY_ACTION=rewrite
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_abort yes 'still names `specs/005-own-feature`'
case "$(last_comment)" in *'post its marker'*) : ;; *) err "the comment does not name the release path" ;; esac
grep -qF 'bureau-branch: <directory name>' "$REPO_ROOT/docs/troubleshooting.md" || err "troubleshooting does not describe the marker comment"
if [ -z "$row_errors" ]; then
  echo "PASS S11 the ticket's own directory without a marker is held, and says how to release it"
  export BUREAU_STUB_REVIEW_COMMENTS='[{"body":"<!-- bureau-branch: 005-own-feature -->","createdAt":"2026-01-05T00:00:00Z"}]'
  run_pipeline spec-pipeline.sh EXP-910; set +e
  expect_proceed specs/005-own-feature/
  finish_row "P7 after the marker comment is posted, the next run proceeds on that directory"
else
  finish_row "S11 the ticket's own directory without a marker is held, and says how to release it"
fi

# P8 — comments with an empty and a null body, newer than the marker, are skipped.
setup detached '{"feature_directory":"specs/004-own-feature"}' 004-own-feature
export PR4_SPECIFY_ACTION=leave BUREAU_STUB_REVIEW_COMMENTS='[{"body":"","createdAt":"2026-01-04T00:00:00Z"},{"body":null,"createdAt":"2026-01-03T00:00:00Z"},{"body":"<!-- bureau-branch: 004-own-feature -->\n**Spec Artifacts — EXP-910**","createdAt":"2026-01-02T00:00:00Z"}]'
run_pipeline spec-pipeline.sh EXP-910; set +e
expect_proceed specs/004-own-feature/
finish_row "P8 empty and null comment bodies do not break the marker read"

if [ "$failed" -gt 0 ]; then
  echo "FAILED rows: $failed" >&2
  exit 1
fi
echo "OK test_spec_feature_json"
