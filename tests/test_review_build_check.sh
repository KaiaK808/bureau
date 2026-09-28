#!/bin/bash
# The review stage's build check runs the project's real check and says so when it ran none.
#
# It used to run `npm run build` only when package.json existed. A repo without package.json
# (Python, Rust) got no check at all, and the review comment still said "**Build**: Passed".
# Now the order is repo.test_command, then scripts/bureau-test.sh, then the npm build (QA's
# first two steps; QA's further fallbacks are not review's). With none of them the comment
# says "not checked" and stderr warns. The command runs in the review worktree under
# pipefail, and files it leaves or changes are named on stderr.
#
# Runs the REAL code-review-pipeline.sh end to end in the harness sandbox (stub gh, stub
# Linear, fake reviewers). Every check command leaves a marker outside the repository, so a
# case proves the command ran, not only that the comment was spelled right. The last case
# puts the old Phase 2 back into the sandbox copy and shows these assertions turn on it.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP

MARKS=$(mktemp -d -t bureau-test.buildcheck.XXXXXXXX)
FAKEBIN="$MARKS/bin"
mkdir -p "$FAKEBIN"
# npm stands in for a real build: it records that it ran and succeeds.
cat > "$FAKEBIN/npm" <<EOF
#!/bin/sh
echo "npm \$*" >> "$MARKS/npm"
EOF
chmod +x "$FAKEBIN/npm"
# teardown returns 1 once a case has already removed its sandbox; under set -e that would
# become the script's status.
trap 'teardown || true; rm -rf "$MARKS"' EXIT

fail() { echo "FAIL $*" >&2; printf '%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }

# $1 = verdict for the fake reviewers, $2 = repo.test_command ('' = unset), $3.. = extras:
# shim (scripts/bureau-test.sh), pkg (package.json), old (the old Phase 2) and split (the
# stage runs from a scripts copy outside the worktree, so SCRIPT_REPO differs from the
# working directory, as for a disposable worker). Leaves LAST_* and $BUILD_LINE.
run_review() {
  local verdict="$1" test_command="$2"; shift 2
  rm -f "$MARKS/tc" "$MARKS/shim" "$MARKS/npm" "$MARKS/stdin" "$MARKS/cwd"
  sandbox_init EXP-701 test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  if [ -n "$test_command" ]; then
    jq -n --arg c "$test_command" '{repo: {test_command: $c}}' > "$SANDBOX/.bureau.json"
  fi
  local extra
  for extra in "$@"; do
    case "$extra" in
      shim) printf '#!/bin/bash\ntouch "%s/shim"\n' "$MARKS" > "$SANDBOX/scripts/bureau-test.sh" ;;
      pkg)  printf '{"name":"fixture","scripts":{"build":"true"}}\n' > "$SANDBOX/package.json" ;;
      split) rm -rf "$MARKS/elsewhere"; mkdir -p "$MARKS/elsewhere"
             cp -R "$SCRIPTS_DIR" "$MARKS/elsewhere/scripts"
             printf '#!/bin/bash\ntouch "%s/shim"\n' "$MARKS" > "$MARKS/elsewhere/scripts/bureau-test.sh"
             SCRIPTS_DIR="$MARKS/elsewhere/scripts" ;;
      old)  python3 - "$SCRIPTS_DIR/code-review-pipeline.sh" <<'PY'
import sys
path = sys.argv[1]
src = open(path).read()
start = src.index('echo "Phase 2/3: build check"\n')
end = src.index('echo ""\necho "Phase 3/3: post review + route"')
old_phase2 = '''echo "Phase 2/3: build check"
BUILD_OK=true
if [ -f "package.json" ]; then
  echo "  Running build..."
  if npm run build 2>&1 | tail -20; then echo "  Build passed"
  else echo "  Build failed"; BUILD_OK=false; fi
fi

'''
src = src[:start] + old_phase2 + src[end:]
new_line = '**Build**: $BUILD_STATUS\n'
assert src.count(new_line) == 1, "Build line not found"
src = src.replace(new_line, '**Build**: $([ "$BUILD_OK" = true ] && echo "Passed" || echo "FAILED")\n')
open(path, 'w').write(src)
PY
            ;;
    esac
  done
  printf 'Review checked.\n```json\n{"verdict":"%s","bugs":0,"security_issues":0,"findings":[],"summary":"fixture"}\n```\n' \
    "$verdict" > "$SANDBOX/verdict.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/verdict.txt"
  export BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  # Stop before merge: every case ends at the posted review, none reaches a merge.
  export BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=0
  # A line waits on stdin: an unattended check must never read the stage's input.
  PATH="$FAKEBIN:$PATH" run_pipeline code-review-pipeline.sh EXP-701 <<< 'caller-stdin'
  # A non-zero stage keeps its temp dir for inspection; this test does not need it.
  rm -rf "$(printf '%s\n' "$LAST_STDERR" | sed -n 's/^code-review failed .*preserved at //p')"
  BUILD_LINE=$(grep -E '^\*\*Build\*\*: ' "$SANDBOX/gh_calls.log" || true)
  VERDICT_LINE=$(grep -E '^\*\*Verdict\*\*: ' "$SANDBOX/gh_calls.log" || true)
  [ "$(printf '%s\n' "$BUILD_LINE" | grep -c .)" = 1 ] || fail "expected one posted Build line, got: '$BUILD_LINE'"
}

# 1 — repo.test_command green: it ran, and the review says Passed.
run_review APPROVE "echo lots of test output; touch '$MARKS/tc'; if read -r l; then touch '$MARKS/stdin'; fi"
[ -f "$MARKS/tc" ] || fail "repo.test_command did not run"
[ ! -e "$MARKS/stdin" ] || fail "repo.test_command read the stage's stdin"
[ "$BUILD_LINE" = '**Build**: Passed' ] || fail "green test_command posted '$BUILD_LINE'"
[ "$VERDICT_LINE" = '**Verdict**: APPROVE' ] || fail "green test_command changed the verdict: '$VERDICT_LINE'"
[ "$LAST_RC" = 20 ] || fail "green APPROVE should stop before merge with 20, got $LAST_RC"
teardown
echo "PASS a green repo.test_command runs without stdin and is reported as Passed"

# 2 — repo.test_command red: the floor applies to APPROVE and to BLOCK; a failure inside a
# pipe of the command counts too (pipefail, as in QA's eval).
run_review APPROVE "echo lots of test output; touch '$MARKS/tc'; exit 3"
[ -f "$MARKS/tc" ] || fail "red repo.test_command did not run"
[ "$BUILD_LINE" = '**Build**: FAILED' ] || fail "red test_command posted '$BUILD_LINE'"
[ "$VERDICT_LINE" = '**Verdict**: REQUEST_CHANGES' ] || fail "red build left '$VERDICT_LINE' for an APPROVE"
assert_calls_include 'move_issue' 'red build routes back to Build'
teardown
run_review APPROVE 'echo "1 failed"; false | cat'
[ "$BUILD_LINE" = '**Build**: FAILED' ] || fail "a failure inside a pipe posted '$BUILD_LINE'"
[ "$VERDICT_LINE" = '**Verdict**: REQUEST_CHANGES' ] || fail "a failure inside a pipe left '$VERDICT_LINE'"
teardown
run_review BLOCK "touch '$MARKS/tc'; exit 3"
[ "$BUILD_LINE" = '**Build**: FAILED' ] || fail "red test_command under BLOCK posted '$BUILD_LINE'"
[ "$VERDICT_LINE" = '**Verdict**: BLOCK' ] || fail "red build softened a BLOCK into '$VERDICT_LINE'"
[ "$LAST_RC" = 25 ] || fail "BLOCK with a red build should end with 25, got $LAST_RC"
teardown
echo "PASS a red repo.test_command is FAILED and goes through apply_build_failure"

# 3 — nothing to run: said on stderr and in the comment, never Passed, verdict untouched.
run_review APPROVE ""
case "$BUILD_LINE" in '**Build**: not checked ('*) ;; *) fail "no check posted '$BUILD_LINE'" ;; esac
case "$LAST_STDERR" in *'WARN: build not checked'*) ;; *) fail "no check left no warning on stderr" ;; esac
[ "$VERDICT_LINE" = '**Verdict**: APPROVE' ] || fail "an unchecked build changed the verdict: '$VERDICT_LINE'"
[ ! -e "$MARKS/npm" ] || fail "npm ran without package.json"
teardown
echo "PASS no check command is reported as not checked and leaves the verdict alone"

# 4 — order: test_command before the shim before package.json.
run_review APPROVE "touch '$MARKS/tc'" shim pkg
[ -f "$MARKS/tc" ] && [ ! -e "$MARKS/shim" ] && [ ! -e "$MARKS/npm" ] \
  || fail "with all three present, not only repo.test_command ran (tc/shim/npm: $(ls "$MARKS"))"
teardown
run_review APPROVE "" shim pkg
[ -f "$MARKS/shim" ] && [ ! -e "$MARKS/npm" ] || fail "the shim did not win over package.json ($(ls "$MARKS"))"
[ "$BUILD_LINE" = '**Build**: Passed' ] || fail "green shim posted '$BUILD_LINE'"
teardown
run_review APPROVE "" pkg
grep -qx 'npm run build' "$MARKS/npm" 2>/dev/null || fail "package.json alone did not run npm run build"
[ "$BUILD_LINE" = '**Build**: Passed' ] || fail "green npm build posted '$BUILD_LINE'"
teardown
echo "PASS repo.test_command, then scripts/bureau-test.sh, then npm run build"

# 5 — new files git does not ignore and changed tracked files are named on stderr, each with
# its own advice, and nothing else changes; a clean check gets no warning.
UNTRACKED_WARN='left new files git does not ignore; add them to .gitignore'
TRACKED_WARN='changed tracked files; it must not write to them'
run_review APPROVE "mkdir -p reports; echo x > reports/junit.xml"
case "$LAST_STDERR" in *"$UNTRACKED_WARN"*'reports/junit.xml'*) ;;
  *) fail "an unignored build artifact was not named on stderr" ;; esac
case "$LAST_STDERR" in *"$TRACKED_WARN"*) fail "a new file was reported as a tracked change" ;; esac
[ "$BUILD_LINE" = '**Build**: Passed' ] || fail "an unignored artifact changed the Build line: '$BUILD_LINE'"
[ "$VERDICT_LINE" = '**Verdict**: APPROVE' ] || fail "an unignored artifact changed the verdict: '$VERDICT_LINE'"
[ "$LAST_RC" = 20 ] || fail "an unignored artifact changed the exit to $LAST_RC"
teardown
run_review APPROVE "echo more >> change.txt"
case "$LAST_STDERR" in *"$TRACKED_WARN"*' M change.txt'*) ;;
  *) fail "a changed tracked file was not named on stderr" ;; esac
case "$LAST_STDERR" in *"$UNTRACKED_WARN"*) fail "a tracked change was sent to .gitignore" ;; esac
[ "$BUILD_LINE" = '**Build**: Passed' ] || fail "a tracked change changed the Build line: '$BUILD_LINE'"
teardown
run_review APPROVE "true"
case "$LAST_STDERR" in *'WARN: the build check'*) fail "a clean check was warned about" ;; esac
teardown
echo "PASS new and changed files are named on stderr, each with its own advice, nothing else changes"

# 6 — the check runs in the review worktree, not in the checkout the scripts come from.
run_review APPROVE "pwd -P > '$MARKS/cwd'" split
[ "$(cat "$MARKS/cwd" 2>/dev/null)" = "$(cd "$SANDBOX" && pwd -P)" ] \
  || fail "repo.test_command ran in '$(cat "$MARKS/cwd" 2>/dev/null)', not in the worktree"
teardown
run_review APPROVE "" split
[ ! -e "$MARKS/shim" ] || fail "the shim was taken from the scripts checkout, not from the worktree"
case "$BUILD_LINE" in '**Build**: not checked ('*) ;; *) fail "split run without a worktree check posted '$BUILD_LINE'" ;; esac
teardown
echo "PASS the check and the shim are resolved in the worktree, not in SCRIPT_REPO"

# 7 — negative control: the old Phase 2 in the same sandbox fails the checks above.
run_review APPROVE "touch '$MARKS/tc'" old
[ ! -e "$MARKS/tc" ] || fail "negative control: the old build check ran repo.test_command"
[ "$BUILD_LINE" = '**Build**: Passed' ] || fail "negative control: the old code posted '$BUILD_LINE'"
teardown
echo "PASS negative control: the old build check ignores repo.test_command and still says Passed"
