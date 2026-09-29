#!/bin/bash
# Every stage that reads the ticket's spec uses the ticket's own spec directory,
# never a decoy that one of the old matchers picked:
#   - 001-aaa-decoy sorts first among the "001-" directories. The old numeric-
#     prefix-first matcher (implement) handed it to every 001- branch.
#   - 001-branch has the slug "branch", a substring of the branch
#     001-test-branch, and sorts before 001-test-branch. The old loose substring
#     match (qa, review, copy, ux, spec-review) took it.
#   - Both decoys are newer than the ticket's directory. The old "newest
#     directory" choice (spec stage; spec-review and ux as a fallback) took one.
# Runs the REAL stages in the harness sandbox (stub Linear and gh, fake agent)
# and checks what each stage printed, passed on and told the agent.
set -uo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP

OWN="specs/001-test-branch/"
fail() {
  echo "FAIL [$1] $2" >&2
  printf '%s\n' "${LAST_STDOUT:-}" | tail -20 >&2
  printf '%s\n' "${LAST_STDERR:-}" | tail -10 >&2
  exit 1
}

# setup <Linear state> — sandbox on branch 001-test-branch with the ticket's spec
# directory (oldest) and two decoys (newer), each with spec.md and tasks.md.
setup() {
  sandbox_init EXP-900 001-test-branch
  local d
  for d in 001-aaa-decoy 001-branch; do
    mkdir -p "$SANDBOX/specs/$d"
    printf '# decoy tasks\n- [ ] T001 decoy task\n' > "$SANDBOX/specs/$d/tasks.md"
    printf '# decoy spec\n' > "$SANDBOX/specs/$d/spec.md"
  done
  printf '# ticket spec\n' > "$SANDBOX/specs/001-test-branch/spec.md"
  touch -t 202001010000 "$SANDBOX/specs/001-test-branch" "$SANDBOX/specs/001-test-branch/"*
  touch -t 203001010000 "$SANDBOX/specs/001-aaa-decoy" "$SANDBOX/specs/001-aaa-decoy/"* \
                        "$SANDBOX/specs/001-branch" "$SANDBOX/specs/001-branch/"*
  git -C "$SANDBOX" add specs
  git -C "$SANDBOX" commit -q -m 'fixture specs'
  git -C "$SANDBOX" push -q origin 001-test-branch
  # The stub's build_spec_context returns nothing; the real one lists the spec
  # directory's files in the prompt, which is what this test reads.
  sed -n '/^build_spec_context() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  grep -q '^build_spec_context() {' "$SCRIPTS_DIR/bureau-config.sh" || fail setup "real build_spec_context not found"
  PROMPTS="$SANDBOX/prompts.log"
  export FAKE_CLAUDE_PROMPT_LOG="$PROMPTS"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_filler.txt"
  export BUREAU_STUB_ISSUE_STATE="$1"
  unset FAKE_CLAUDE_COMMIT_ON_ITERS
}

# check_prompts <label> — the agent was told about the ticket's directory and
# about no decoy.
check_prompts() {
  [ -s "$PROMPTS" ] || fail "$1" "the stage never called the agent"
  grep -qF "$OWN" "$PROMPTS" || fail "$1" "no prompt names $OWN"
  if grep -qE 'specs/001-(aaa-decoy|branch)/' "$PROMPTS"; then
    fail "$1" "a prompt names a decoy: $(grep -oE 'specs/001-(aaa-decoy|branch)/' "$PROMPTS" | sort -u | tr '\n' ' ')"
  fi
}

# check_stdout <label> <prefix> — the stage printed "<prefix> …/specs/001-test-branch/…".
check_stdout() {
  local line
  line=$(printf '%s\n' "$LAST_STDOUT" | grep -F "$2" | head -1 || true)
  case "$line" in
    *"$OWN"*) : ;;
    *) fail "$1" "expected '${2} …${OWN}…', got '${line:-<nothing>}'" ;;
  esac
}

# 1 — spec: plan, tasks, crosscheck and digest use the directory from feature.json.
setup Triage
export BUREAU_STUB_LABELS='["lane-2"]'
mkdir -p "$SANDBOX/.specify"
printf '{"feature_directory":"specs/001-test-branch"}\n' > "$SANDBOX/.specify/feature.json"
run_pipeline spec-pipeline.sh EXP-900; set +e
check_prompts spec
grep -qE "^crosscheck_open_prs	EXP-900	${OWN}tasks.md$" "$SANDBOX/calls.log" \
  || fail spec "crosscheck got: $(grep crosscheck_open_prs "$SANDBOX/calls.log" || echo '<no call>')"
unset BUREAU_STUB_LABELS
teardown
echo "PASS spec stage works on the directory from .specify/feature.json"

# 2 — spec review: prints and reviews the ticket's directory.
setup 'Spec Review'
run_pipeline spec-review-pipeline.sh EXP-900; set +e
check_stdout spec-review 'Spec dir:'
check_prompts spec-review
teardown
echo "PASS spec review locates the ticket's spec directory"

# 3 — UX/design.
setup Design
run_pipeline ux-pipeline.sh EXP-900; set +e
check_stdout ux 'Spec dir:'
check_prompts ux
teardown
echo "PASS ux locates the ticket's spec directory"

# 4 — implement: the tasks.md it works through, and the prompt.
setup Build
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS=1
run_pipeline implement-pipeline.sh EXP-900; set +e
check_stdout implement 'Found tasks:'
check_prompts implement
teardown
echo "PASS implement works through the ticket's tasks.md"

# 5 — QA (a green test command, so QA hands the tasks to the agent).
setup QA
export BUREAU_STUB_STATE_QA=state-qa
jq -n '{repo: {test_command: "true"}}' > "$SANDBOX/.bureau.json"
printf '```json\n{"status":"GREEN","tests_added":0,"tests_failing":0,"coverage_notes":"fixture"}\n```\n' > "$SANDBOX/qa.txt"
export FAKE_CLAUDE_FIXTURES="$SANDBOX/qa.txt"
run_pipeline qa-pipeline.sh EXP-900; set +e
check_prompts qa
unset BUREAU_STUB_STATE_QA
teardown
echo "PASS qa checks against the ticket's spec"

# 6 — code review: the specialists' grounding.
setup 'Build Review'
printf 'Review checked.\n```json\n{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"fixture"}\n```\n' > "$SANDBOX/verdict.txt"
export FAKE_CLAUDE_FIXTURES="$SANDBOX/verdict.txt" GH_STUB_EXISTING_PR=99 BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=0
run_pipeline code-review-pipeline.sh EXP-900; set +e
rm -rf "$(printf '%s\n' "$LAST_STDERR" | sed -n 's/^code-review failed .*preserved at //p')"
check_prompts code-review
unset GH_STUB_EXISTING_PR BUREAU_NO_MERGE BUREAU_STOP_REQUESTED
teardown
echo "PASS code review grounds the specialists in the ticket's spec"

# 7 — copy.
setup Copy
export BUREAU_STUB_STATE_COPY=state-copy
printf 'BUREAU_LABEL_NEEDS_COPY_NAME=needs-copy\n' >> "$SCRIPTS_DIR/bureau-config.sh"
run_pipeline copy-pipeline.sh EXP-900; set +e
check_prompts copy
unset BUREAU_STUB_STATE_COPY
teardown
echo "PASS copy works from the ticket's spec"

# 8–10 — a branch that fits two directories equally (001-test fits 001-test-alpha and
# 001-test-beta alike): implement, spec review and UX stop with 13, name both candidates,
# set needs-human and route back, instead of taking a first or newest directory or
# claiming that tasks.md is missing.
ambiguous() {  # <label> <state> <stage script> <state the ticket goes back to>
  setup "$2"
  export BUREAU_STUB_BRANCH=001-test
  mv "$SANDBOX/specs/001-test-branch" "$SANDBOX/specs/001-test-alpha"
  cp -R "$SANDBOX/specs/001-test-alpha" "$SANDBOX/specs/001-test-beta"
  git -C "$SANDBOX" checkout -q -b 001-test
  git -C "$SANDBOX" add -A specs
  git -C "$SANDBOX" commit -q -m 'two candidates'
  git -C "$SANDBOX" push -q origin 001-test
  run_pipeline "$3" EXP-900; set +e
  [ "$LAST_RC" = 13 ] || fail "$1" "expected exit 13, got $LAST_RC"
  assert_calls_include "move_issue.*$4\$" "$1 routes back" || fail "$1" "no move back to $4"
  assert_calls_include 'add_issue_label.*needs-human' "$1 sets needs-human" || fail "$1" "needs-human not set"
  local comment
  comment=$(grep '^post_comment' "$SANDBOX/calls.log" | tail -1)
  case "$comment" in
    *'fits more than one spec directory'*'`001-test-alpha`'*) : ;;
    *'fits more than one spec directory'*'`001-test-beta`'*) : ;;
    *) fail "$1" "comment does not name the candidates: $comment" ;;
  esac
  case "$comment" in *'`001-test-alpha`'*) ;; *) fail "$1" "comment misses 001-test-alpha: $comment" ;; esac
  case "$comment" in *'`001-test-beta`'*) ;; *) fail "$1" "comment misses 001-test-beta: $comment" ;; esac
  case "$comment" in *'remove `needs-human`'*) ;; *) fail "$1" "comment lost its literal text: $comment" ;; esac
  case "$comment" in *'tasks.md'*'missing'*|*'no tasks.md'*) fail "$1" "comment still claims a missing tasks.md: $comment" ;; esac
  [ ! -s "$PROMPTS" ] || fail "$1" "the agent was called although no spec directory was clear"
  unset BUREAU_STUB_BRANCH
  teardown
}
ambiguous implement Build implement-pipeline.sh state-spec
echo "PASS implement names the tied spec directories, sets needs-human and stops instead of guessing"
ambiguous spec-review 'Spec Review' spec-review-pipeline.sh state-spec
echo "PASS spec review names the tied spec directories and stops"
ambiguous ux Design ux-pipeline.sh state-spec-review
echo "PASS ux names the tied spec directories and stops"

echo "OK test_spec_dir_stages"
