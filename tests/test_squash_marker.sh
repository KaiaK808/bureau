#!/bin/bash
# No commit in the squash range may carry a CI suppressor past a stage, and the stages add none.
#
# Three layers, each run for real:
#   1. squash-marker-check.sh against a git fixture: every entry of ci-skip-markers.txt is
#      found in subject and body, commits already on main are not, and anything it cannot
#      read is "not checked" (exit 2), never clean.
#   2. The implement stage (harness, real pipeline, real check_squash_range): a clean run
#      writes no `[skip ci]` and no empty checkpoint commit and still pushes its work; a run
#      whose commit carries a marker ends in CI_MARKER, stays out of QA/Build Review, labels
#      needs-human, comments on the PR — and still pushes, so the work survives.
#   3. The QA routing block cut out of qa-pipeline.sh: a marker turns GREEN into NEEDS_HUMAN.
#      Negative control: the routing without the check sends the same branch to Build Review.
set -euo pipefail

source "$(dirname "$0")/lib/harness.sh"
SCRIPTS="$REPO_ROOT/templates/scripts"
FX=$(mktemp -d -t bureau-test.squash.XXXXXXXX)
trap 'teardown || true; rm -rf "$FX"' EXIT

fail() { echo "FAIL $*" >&2; [ -n "${OUT:-}" ] && printf '%s\n' "$OUT" | sed 's/^/  | /' >&2; exit 1; }

# --- 1. the script ------------------------------------------------------------
R="$FX/repo"
git init -q --bare "$FX/origin.git"
git init -q -b main "$R"
git -C "$R" config user.email test@bureau
git -C "$R" config user.name "Bureau Test"
git -C "$R" remote add origin "$FX/origin.git"
git -C "$R" commit -q --allow-empty -m init
# Already on main, so outside every squash range below: must never be reported.
git -C "$R" commit -q --allow-empty -m "old main commit [skip ci]"
git -C "$R" push -q origin main

mkdir -p "$FX/serial" "$FX/bin"
cp "$SCRIPTS/bureau-env.sh" "$SCRIPTS/ci-skip-markers.txt" "$FX/serial/"
python3 - "$SCRIPTS/squash-marker-check.sh" "$FX/serial/squash-marker-check.sh" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text()
start = text.index('batch=$(mktemp ')
end = text.index('\n  # The raw object:', start)
text = text[:start] + '''for sha in $commits; do
  count=$((count + 1))
  if raw=$(git cat-file commit "$sha"); then :; else
    not_checked "reading commit $sha failed (exit code $?)"
  fi
''' + text[end:]
text = text.replace('done < "$batch/messages"', 'done')
Path(sys.argv[2]).write_text(text)
PY
export SQUASH_REAL_GIT="$(command -v git)" SQUASH_BATCH_CALLS="$FX/batch.calls"
cat > "$FX/bin/git" <<'EOF'
#!/bin/bash
if [ "$1" = cat-file ]; then
  printf '%s\n' "$2" >> "$SQUASH_BATCH_CALLS"
  if [ "${SQUASH_SCRATCH_ERROR:-}" = truncate-commits ]; then
    batch=$(cat "$SQUASH_BATCH_PATH")
    IFS= read -r sha < "$batch/commits"
    printf '%s\n' "$sha" > "$batch/commits"
  elif [ "${SQUASH_SCRATCH_ERROR:-}" = write-messages ]; then
    batch=$(cat "$SQUASH_BATCH_PATH")
    rm "$batch/messages"
    mkdir "$batch/messages"
  fi
  if [ -n "${SQUASH_BATCH_ERROR:-}" ]; then
    exec python3 - "$@" <<'PY'
import os
import subprocess
import sys

sha = subprocess.check_output([os.environ['SQUASH_REAL_GIT'], 'rev-parse', 'HEAD']).strip()
mode = os.environ['SQUASH_BATCH_ERROR']
raw = b'tree unused\n\nmessage\n'
header = sha + b' commit ' + str(len(raw)).encode() + b'\n'
if mode == 'exit': sys.exit(1)
elif mode == 'missing': data = sha + b' missing\n'
elif mode == 'type': data = header.replace(b'commit', b'blob') + raw + b'\n'
elif mode == 'sha': data = b'0' * len(sha) + header[len(sha):] + raw + b'\n'
elif mode == 'size': data = sha + b' commit invalid\n' + raw + b'\n'
elif mode == 'truncated': data = header + raw[:-2]
elif mode == 'separator': data = header + raw + b'x'
elif mode == 'extra': data = header + raw + b'\nextra'
elif mode == 'late-exit':
    sys.stdout.buffer.write(header + raw + b'\n'); sys.exit(1)
else: raise AssertionError(mode)
sys.stdout.buffer.write(data)
PY
  fi
fi
exec "$SQUASH_REAL_GIT" "$@"
EOF
chmod +x "$FX/bin/git"

check() {  # $1 = base (optional); runs the real script in $R under /bin/bash; sets OUT, RC
  local serial_rc calls
  : > "$SQUASH_BATCH_CALLS"
  set +e
  (cd "$R" && PATH="$FX/bin:$PATH" /bin/bash "$SCRIPTS/squash-marker-check.sh" "$@") > "$FX/current.out" 2>&1
  RC=$?
  (cd "$R" && /bin/bash "$FX/serial/squash-marker-check.sh" "$@") > "$FX/serial.out" 2>&1
  serial_rc=$?
  set -e
  OUT=$(cat "$FX/current.out")
  [ "$RC" = "$serial_rc" ] && cmp -s "$FX/current.out" "$FX/serial.out" || fail "batch changed output or exit code"
  calls=$(wc -l < "$SQUASH_BATCH_CALLS" | tr -d ' ')
  [ "$calls" -le 1 ] || fail "more than one cat-file call: $calls"
  if [ "$calls" = 1 ]; then grep -qx -- --batch "$SQUASH_BATCH_CALLS" || fail "cat-file was not batched"; fi
}
probe() {  # a fresh branch off main with one commit: $1 = subject, $2 = body (optional)
  git -C "$R" checkout -q -B probe main
  if [ -n "${2:-}" ]; then
    git -C "$R" commit -q --allow-empty -m "$1" -m "$2"
  else
    git -C "$R" commit -q --allow-empty -m "$1"
  fi
}

probe "EXP-1: clean work"
check
[ "$RC" = 0 ] || fail "clean range: exit $RC"
case "$OUT" in *"1 commit(s) read, none carries"*) ;; *) fail "clean range not reported as read" ;; esac
echo "PASS a clean range is exit 0, and a marker already on main is outside the range"

n=0
while IFS= read -r entry || [ -n "$entry" ]; do
  [ -n "$entry" ] || continue
  n=$((n + 1))
  probe "EXP-1: work $entry"
  check
  [ "$RC" = 3 ] || fail "subject with '$entry': exit $RC, wanted 3"
  case "$OUT" in *"subject  $entry"*) ;; *) fail "finding for '$entry' does not name place and entry" ;; esac
done < "$SCRIPTS/ci-skip-markers.txt"
[ "$n" -ge 7 ] || fail "only $n entries in ci-skip-markers.txt"
probe "EXP-1: clean subject" "Longer body.

skip-checks: true"
check
[ "$RC" = 3 ] || fail "marker in the body: exit $RC, wanted 3"
case "$OUT" in *"body  skip-checks: true"*) ;; *) fail "body finding not named" ;; esac
echo "PASS every one of the $n list entries is found in a subject, and a trailer in the body"

git -C "$R" checkout -q main
check
[ "$RC" = 0 ] || fail "empty range: exit $RC"
check origin/does-not-exist
[ "$RC" = 2 ] || fail "unresolvable base: exit $RC, wanted 2"
mkdir -p "$FX/nolist"
cp "$SCRIPTS/squash-marker-check.sh" "$FX/nolist/"
set +e; OUT=$(cd "$R" && /bin/bash "$FX/nolist/squash-marker-check.sh" 2>&1); RC=$?; set -e
[ "$RC" = 2 ] || fail "missing list: exit $RC, wanted 2"
case "$OUT" in *"NOT CHECKED"*) ;; *) fail "missing list not reported as not checked" ;; esac
echo "PASS an empty range is clean; a missing list or base is 'not checked' (exit 2), never clean"

probe 'Unicode and quoted text' $'r\303\251sum\303\251\n\nlooks like a header: commit 0\n\nlast line'
git -C "$R" commit -q --allow-empty -m 'second commit'
check
[ "$RC" = 0 ] && grep -qx -- --batch "$SQUASH_BATCH_CALLS" || fail "multi-commit range was not read in one batch"
: > "$SQUASH_BATCH_CALLS"
(cd "$R" && PATH="$FX/bin:$PATH" /bin/bash "$FX/serial/squash-marker-check.sh") > "$FX/control.out"
[ "$(wc -l < "$SQUASH_BATCH_CALLS" | tr -d ' ')" = 2 ] || fail "counter missed serial cat-file calls"
cmp -s "$FX/current.out" "$FX/control.out" || fail "serial counter control changed output"
probe 'read-error probe'
for mode in exit missing type sha size truncated separator extra late-exit; do
  set +e
  OUT=$(cd "$R" && PATH="$FX/bin:$PATH" SQUASH_BATCH_ERROR="$mode" /bin/bash "$SCRIPTS/squash-marker-check.sh" 2>&1)
  RC=$?
  set -e
  [ "$RC" = 2 ] || fail "batch $mode: exit $RC, wanted 2"
  case "$OUT" in *"NOT CHECKED"*) ;; *) fail "batch $mode passed as checked" ;; esac
done
echo "PASS batch output matches serial fixtures byte-for-byte; corrupt frames and read errors fail closed"

export SQUASH_REAL_MKTEMP="$(command -v mktemp)" SQUASH_REAL_PYTHON="$(command -v python3)"
export SQUASH_BATCH_PATH="$FX/batch.path"
cat > "$FX/bin/mktemp" <<'EOF'
#!/bin/bash
[ -n "${SQUASH_SCRATCH_ERROR:-}" ] || exec "$SQUASH_REAL_MKTEMP" "$@"
batch=$("$SQUASH_REAL_MKTEMP" "$@") || exit $?
case "$SQUASH_SCRATCH_ERROR" in
  create-messages) mkdir "$batch/messages" ;;
  write-commits) mkdir "$batch/commits" ;;
  write-objects) mkdir "$batch/objects" ;;
esac
printf '%s\n' "$batch" > "$SQUASH_BATCH_PATH"
printf '%s\n' "$batch"
EOF
cat > "$FX/bin/python3" <<'EOF'
#!/bin/bash
[ -n "${SQUASH_SCRATCH_ERROR:-}" ] || exec "$SQUASH_REAL_PYTHON" "$@"
[ "$SQUASH_SCRATCH_ERROR" != write-frames ] || exec 1>&-
"$SQUASH_REAL_PYTHON" "$@" || exit $?
batch=$(cat "$SQUASH_BATCH_PATH")
case "$SQUASH_SCRATCH_ERROR" in
  read-messages) rm "$batch/messages" ;;
  truncate-messages) : > "$batch/messages" ;;
  partial-messages) printf partial > "$batch/messages" ;;
  cancel-*)
    [ -s "$batch/commits" ] && [ -s "$batch/objects" ] && [ -s "$batch/messages" ] || exit 1
    kill -s "${SQUASH_SCRATCH_ERROR#cancel-}" "$SQUASH_CHECK_PID" "$$"
    ;;
esac
EOF
chmod +x "$FX/bin/mktemp" "$FX/bin/python3"
probe 'scratch-I/O probe'
git -C "$R" commit -q --allow-empty -m 'second commit'
for mode in read-messages truncate-messages partial-messages truncate-commits write-commits write-objects write-messages write-frames create-messages; do
  if [ "$mode" = create-messages ]; then git -C "$R" checkout -q main; fi
  set +e
  OUT=$(cd "$R" && PATH="$FX/bin:$PATH" SQUASH_SCRATCH_ERROR="$mode" /bin/bash "$SCRIPTS/squash-marker-check.sh" 2>&1)
  RC=$?
  set -e
  [ "$RC" = 2 ] || fail "scratch $mode: exit $RC, wanted 2"
  case "$OUT" in *"NOT CHECKED"*) ;; *) fail "scratch $mode passed as checked" ;; esac
  case "$OUT" in *"none carries"*) fail "scratch $mode reported clean" ;; esac
  [ ! -e "$(cat "$SQUASH_BATCH_PATH")" ] || fail "scratch $mode left its batch directory"
done
echo "PASS scratch-file creation, writes, reads and truncated ranges fail closed and clean up"

probe 'cancelled batch'
python3 - "$R" "$FX/bin" "$SCRIPTS/squash-marker-check.sh" <<'PY'
import os
from pathlib import Path
import signal
import subprocess
import sys

for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    env = dict(os.environ, PATH=sys.argv[2] + os.pathsep + os.environ['PATH'],
               SQUASH_SCRATCH_ERROR='cancel-' + sig.name[3:])
    child = subprocess.Popen(['/bin/bash', '-c', 'export SQUASH_CHECK_PID=$$; exec /bin/bash "$1"',
                              'check', sys.argv[3]], cwd=sys.argv[1], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = child.communicate(timeout=10)
    except BaseException:
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        child.communicate(timeout=5)
        raise
    assert child.returncode == 128 + sig, (sig.name, child.returncode, out, err)
    batch = Path(Path(os.environ['SQUASH_BATCH_PATH']).read_text().strip())
    assert not batch.exists(), (sig.name, 'batch directory remains', batch)
print('PASS INT, TERM and HUP exit with 128+signal and remove the populated batch directory')
PY

# --- 2. the implement stage ----------------------------------------------------
origin_messages() { git -C "$SANDBOX/.fake-origin.git" log --format=%B main..test-branch; }

sandbox_init "EXP-100" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
export FAKE_CLAUDE_COMMIT_ON_ITERS="1"
export BUREAU_DRY_RUN=0
export BUREAU_IMPL_MAX_ITER=3
run_implement_pipeline
OUT="$LAST_STDOUT"
assert_eq 0 "$LAST_RC" "clean run exit code"
assert_match 'terminal status=COMPLETE' "$LAST_STDOUT" "clean run completes"
assert_match 'squash-marker-check: origin/main\.\.HEAD, [0-9]+ commit\(s\) read, none carries' "$LAST_STDOUT" "the check ran"
assert_calls_include '^move_issue.*state-build-review$' "clean run handed on"
msgs=$(origin_messages)
case "$msgs" in *"fake-claude iter 1 progress"*) ;; *) fail "the iteration commit did not reach origin" ;; esac
case "$msgs" in *"[skip ci]"*) fail "the stage wrote [skip ci] into a commit message: $msgs" ;; esac
case "$msgs" in *"checkpoint (CI re-trigger)"*) fail "the stage still writes the empty checkpoint commit" ;; esac
echo "PASS a clean implement run writes no marker and no checkpoint commit, pushes its work and hands on"
teardown

sandbox_init "EXP-101" "test-branch"
export FAKE_CLAUDE_COMMIT_MSG="EXP-101: add the thing [skip ci]"
export GH_STUB_EXISTING_PR=7
run_implement_pipeline
OUT="$LAST_STDOUT"
unset FAKE_CLAUDE_COMMIT_MSG GH_STUB_EXISTING_PR
assert_match 'terminal status=CI_MARKER' "$LAST_STDOUT" "marker run halts"
assert_calls_exclude '^move_issue.*state-build-review' "marker run not handed to Build Review"
assert_calls_exclude '^move_issue.*state-qa' "marker run not handed to QA"
assert_calls_include 'add_issue_label.*needs-human' "marker run labelled needs-human"
assert_calls_include 'post_comment.*Halted before hand-off' "summary names the halt"
assert_file_contains 'Squash-range check:' "$SANDBOX/calls.log" "summary carries the report"
grep -q "$(printf 'gh\tpr\tcomment\t7\t--body-file\t-')" "$SANDBOX/gh_calls.log" \
  || fail "the report was not posted on the PR"
case "$(origin_messages)" in *"add the thing [skip ci]"*) ;; *) fail "the halted run did not push its work" ;; esac
echo "PASS a marker in a commit halts the hand-off as CI_MARKER, reports on ticket and PR, and keeps the work"
teardown

sandbox_init "EXP-102" "test-branch"
rm "$SCRIPTS_DIR/ci-skip-markers.txt"   # the check cannot read its list
run_implement_pipeline
OUT="$LAST_STDOUT"
assert_match 'terminal status=CI_MARKER' "$LAST_STDOUT" "unreadable check halts"
assert_calls_exclude '^move_issue.*state-build-review' "unchecked range not handed on"
assert_file_contains 'could not be checked \(exit code 2\)' "$SANDBOX/calls.log" "summary says the range was not checked"
echo "PASS a range the check could not read halts like a finding, never passes as clean"
teardown

# --- 3. the QA routing block ---------------------------------------------------
cat > "$R/.bureau.json" <<'EOF'
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
QA_BLOCK=$(awk '/^QA_ESCALATION_REASON="QA flagged NEEDS_HUMAN"$/ { f = 1 } f { print } f && /^esac$/ { exit }' \
  "$SCRIPTS/qa-pipeline.sh")
case "$QA_BLOCK" in
  *'check_squash_range origin/main'*'case "$STATUS" in'*'esac') ;;
  *) fail "the QA routing block no longer has the expected shape" ;;
esac
ROUTING_ONLY=$(printf '%s\n' "$QA_BLOCK" | awk '/^case "\$STATUS" in$/ { f = 1 } f { print }')
QA_ESCALATION_DEFAULT='QA_ESCALATION_REASON="QA flagged NEEDS_HUMAN"'

mkdir -p "$FX/bin"
printf '#!/bin/bash\nprintf "gh %%s\\n" "$*" >> "%s/gh.log"\n' "$FX" > "$FX/bin/gh"
chmod +x "$FX/bin/gh"

qa_route() {  # $1 = block to run; the branch under test is checked out in $R
  rm -f "$FX/qa.log" "$FX/gh.log"
  set +e
  OUT=$(cd "$R" && PATH="$FX/bin:$PATH" /bin/bash -c "
    set -euo pipefail
    source '$SCRIPTS/bureau-config.sh'
    post_comment()    { printf 'post_comment %s\n' \"\$1\" >> '$FX/qa.log'; }
    move_issue()      { printf 'move_issue %s %s\n' \"\$1\" \"\$2\" >> '$FX/qa.log'; }
    add_issue_label() { printf 'add_issue_label %s %s\n' \"\$1\" \"\$2\" >> '$FX/qa.log'; }
    log_escalation()  { printf 'log_escalation %s\n' \"\$4\" >> '$FX/qa.log'; }
    ISSUE=EXP-7 BRANCH=probe STATUS=GREEN SUMMARY='suite green' QA_LOG_PATH=logs/qa.log
    $1
  " 2>&1)
  RC=$?
  set -e
  QA_LOG=$(cat "$FX/qa.log" 2>/dev/null || true)
}

probe "EXP-7: tests"
qa_route "$QA_BLOCK"
[ "$RC" = 0 ] || fail "QA block on a clean branch: exit $RC"
case "$QA_LOG" in *"move_issue EXP-7 s6"*) ;; *) fail "a clean GREEN QA run was not moved to Build Review: $QA_LOG" ;; esac
echo "PASS a clean GREEN QA run still goes to Build Review"

probe "EXP-7: tests [ci skip]"
qa_route "$QA_BLOCK"
[ "$RC" = 0 ] || fail "QA block on a marker branch: exit $RC"
case "$QA_LOG" in *"move_issue"*) fail "a GREEN run with a marker was routed on: $QA_LOG" ;; esac
case "$QA_LOG" in *"add_issue_label EXP-7 needs-human"*"log_escalation squash-range check found"*) ;; *) fail "the marker did not become needs-human with a named reason: $QA_LOG" ;; esac
grep -q '^gh pr comment' "$FX/gh.log" 2>/dev/null || grep -q '^gh pr list --head probe' "$FX/gh.log" \
  || fail "comment_on_branch_pr was not called"
echo "PASS a marker turns a GREEN QA run into needs-human, with the finding as the reason"

qa_route "$QA_ESCALATION_DEFAULT
$ROUTING_ONLY"
case "$QA_LOG" in *"move_issue EXP-7 s6"*) ;; *) fail "negative control: routing without the check did not pass the marker branch on, so the test proves nothing" ;; esac
echo "PASS negative control: the routing without the check sends the same branch to Build Review"
