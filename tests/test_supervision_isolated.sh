#!/bin/bash
# bureau-supervision.py starts with `python3 -I` at every call of the stages (v3.2), and so does
# every other start of Python in templates/scripts.
#
# The review stage calls it with the branch's worktree as its working directory (check, reuse,
# stop, stop --merge-gate-wait), the worker's cleanup too (checkpoint), and the bounded tick from
# the checkout it runs in (check, workspace). Started without -I, Python puts the operator's
# PYTHONPATH entries before the standard library, and an empty entry (PYTHONPATH=":/opt/lib") or
# a relative one names the working directory: an argparse.py, json.py or subprocess.py the branch
# committed to its root was imported in place of the standard library and ran with the stage's
# environment, the .env keys included. bureau-supervision.py imports only the standard library and
# nothing from its own directory, so -I (no PYTHONPATH, no script directory, no user site) changes
# nothing else; every run below still ends the way it did.
#
# Runs the REAL code-review-pipeline.sh, merge-pipeline.sh and bureau-supervision.py in the harness
# sandbox (Linear: the stub config; GitHub: the harness gh and the pr2 gh double; the models:
# fake_claude.sh), and the REAL bureau-tick.sh with a stub config and worker as in
# tests/test_supervision.sh, each with PYTHONPATH=":" and modules that shadow the standard library
# in the working directory. Each module appends its name and the process's argv to a marker file,
# then hands over to the real module, so a run goes on and the marker names every call it reached.
# The worker's checkpoint call, the tick and reset_worktree's ownership check (bureau-runtime.py
# assert-owner) run end to end in tests/supervision_pipeline_test.py.
#   S  every start of Python in templates/scripts/*.sh carries -I, bureau-supervision.py's eight
#      among them (inline Python is also checked in tests/test_untrusted_env_bureau.sh)
#   1  a --no-merge review records its stop (stop); the next one finds it (check); a run without
#      a stop reuses it (reuse)
#   2  the inline merge's gate is not yet decided (stop --merge-gate-wait), then blocked (stop)
#   3  a bounded tick of the review stage (check, workspace)
#   4  the app actions (bureau-app.sh doctor, status, setup) and bureau-status.sh --config, whose
#      provider rows start bureau-provider.py --describe
# Control: the same runs with -I removed import the branch's modules at every one of those calls.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP PYTHONPATH
ISSUE=EXP-901
TMP=$(mktemp -d -t bureau-test.supervision-isolated.XXXXXXXX)
TMP=$(cd "$TMP" && pwd -P)
trap 'teardown || true; rm -rf "$TMP"' EXIT
MARK="$TMP/shadow.log"
FAILS=0 SECTION_FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
pass() { if [ "$FAILS" = "$SECTION_FAILS" ]; then echo "PASS $*"; else echo "FAILED $*" >&2; fi; SECTION_FAILS=$FAILS; }
SHADOWED='argparse json subprocess hashlib tempfile uuid'

# shadow <dir>: modules named like the standard library ones bureau-supervision.py imports.
shadow() {
  local m
  for m in $SHADOWED; do
    cat > "$1/$m.py" <<EOF
import os, sys
with open('$MARK', 'a') as _out: _out.write(__name__ + ' ' + ' '.join(sys.argv) + '\n')
_name, _here, _saved = __name__, os.path.dirname(os.path.abspath(__file__)), sys.path[:]
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _here]
del sys.modules[_name]
try:
    import importlib
    sys.modules[_name] = importlib.import_module(_name)
finally:
    sys.path[:] = _saved
EOF
  done
}
# calls <subcommand pattern>: the supervision calls the marker recorded, one line per process.
calls() { local n; n=$(grep -cE "^argparse .*bureau-supervision\.py.*$1" "$MARK" 2>/dev/null) || true; echo "${n:-0}"; }
marker_empty() { [ ! -s "$MARK" ] || fail "$1: the branch's module was imported: $(cut -c1-300 "$MARK")"; rm -f "$MARK"; }
# strip_isolation <scripts dir>: the v3.1 starts, without -I.
strip_isolation() {
  local f
  for f in "$1"/code-review-pipeline.sh "$1"/bureau-tick.sh "$1"/bureau-worker.sh; do
    [ -f "$f" ] || continue
    sed -i.bak -E 's/python3 -I ([^ ]*bureau-supervision\.py)/python3 \1/' "$f"; rm -f "$f.bak"
    ! grep -qE 'python3 -I [^ ]*bureau-supervision\.py' "$f" || { echo "control: -I is still in $f" >&2; exit 1; }
  done
}
# review: the real review stage with an empty PYTHONPATH entry; the harness runs it in the sandbox.
review() {
  rm -f "$SANDBOX/fake_claude_counter"
  export PYTHONPATH=":"
  run_pipeline code-review-pipeline.sh "$ISSUE"
  unset PYTHONPATH
}
new_sandbox() {
  teardown || true
  sandbox_init "$ISSUE" test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  shadow "$SANDBOX"
  git -C "$SANDBOX" add change.txt $(for m in $SHADOWED; do printf '%s.py ' "$m"; done)
  git -C "$SANDBOX" commit -q -m 'the pull request, with modules named like the standard library'
  git -C "$SANDBOX" push -q origin test-branch
  cat > "$SANDBOX/approve.txt" <<'EOF'
Review checked.
```json
{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"Checks passed"}
```
EOF
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt" BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  STOPS="$(git -C "$SANDBOX" rev-parse --path-format=absolute --git-common-dir)/bureau/review-stops.json"
}
record() { jq -c --arg i "$ISSUE" '.[$i] // empty' "$STOPS" 2>/dev/null || true; }
model_calls() { cat "$SANDBOX/fake_claude_counter" 2>/dev/null || echo 0; }

# ── S  every start carries -I ──────────────────────────────────────────────────
SCRIPTS="$REPO_ROOT/templates/scripts"
starts=$(grep -rnE 'python3?( +-[A-Za-z]+)* +[^ ]*bureau-supervision\.py' "$SCRIPTS" | grep -vE ':[0-9]+: *#' || true)
[ "$(printf '%s\n' "$starts" | grep -c .)" -ge 8 ] || fail "S: fewer than the eight known starts of bureau-supervision.py: $starts"
stray=$(printf '%s\n' "$starts" | grep -vE 'python3? +(-[A-Za-z]+ +)*-I ' || true)
[ -z "$stray" ] || fail "S: bureau-supervision.py started without -I: $stray"
# Every other start of Python in the scripts too: each `python3` word outside a comment must carry
# -I among its options. One exception, listed by its text: the operator command quoted in the
# needs-human comment that bureau_reset_refusal_trace writes, which nothing runs.
all_starts=$(python3 -I - "$SCRIPTS" <<'PY'
import pathlib, re, sys
allowed = [('bureau-config.sh', r'Find the run that holds them (\`python3 scripts/bureau-runtime.py status\`)')]
count, stray = 0, []
for path in sorted(pathlib.Path(sys.argv[1]).glob('*.sh')):
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith('#') or any(path.name == name and text in line for name, text in allowed): continue
        for match in re.finditer(r'(?:^|[^A-Za-z0-9_./-])python3?((?: +-[A-Za-z]+)*)(?= |$)', line):
            count += 1
            if '-I' not in match.group(1).split(): stray.append(path.name + ':' + str(number) + ': ' + line.strip())
print(count); print('\n'.join(stray))
PY
)
[ "$(printf '%s\n' "$all_starts" | sed -n 1p)" -ge 20 ] || fail "S: the scan found too few Python starts: $all_starts"
stray=$(printf '%s\n' "$all_starts" | sed 1d)
[ -z "$stray" ] || fail "S: Python started without -I: $stray"
pass "S every start of Python in templates/scripts carries -I ($(printf '%s\n' "$all_starts" | sed -n 1p), $(printf '%s\n' "$starts" | grep -c .) of bureau-supervision.py)"

# ── 1  stop, check, reuse ─────────────────────────────────────────────────────
section1() {  # $1: label
  export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
  python3 -I "$SCRIPTS_DIR/bureau-supervision.py" --repo "$SANDBOX" resume "$ISSUE" >/dev/null
  export BUREAU_NO_MERGE=1; review
  [ "$LAST_RC" = 20 ] || fail "1 $1: the --no-merge review ended $LAST_RC, wanted 20: $LAST_STDERR"
  [ "$(record | jq -r .verdict)" = APPROVE ] || fail "1 $1: the stop recorded no approval: $(record)"
  review
  [ "$LAST_RC" = 20 ] || fail "1 $1: the second --no-merge review ended $LAST_RC, wanted 20"
  grep -q 'Review already approved at the unchanged head; still stopped before merge' <<< "$LAST_STDOUT" \
    || fail "1 $1: the check did not find the recorded stop"
  [ "$(model_calls)" = 0 ] || fail "1 $1: the second --no-merge review paid a model call"
  export BUREAU_NO_MERGE=0; review
  [ "$LAST_RC" = 0 ] || fail "1 $1: the resumed review ended $LAST_RC, wanted 0: $LAST_STDERR"
  grep -q 'Reusing the approval recorded' <<< "$LAST_STDOUT" || fail "1 $1: the resumed review did not reuse the approval"
  [ "$(model_calls)" = 0 ] || fail "1 $1: the resumed review paid a model call"
}
new_sandbox
section1 isolated
marker_empty "1 (stop, check, reuse)"
pass "1 stop, check and reuse import nothing from the branch; the review stops, finds its stop and reuses it"
strip_isolation "$SCRIPTS_DIR"
section1 control
[ "$(calls ' check ')" -ge 2 ] && [ "$(calls ' stop ')" -ge 1 ] && [ "$(calls ' reuse ')" = 1 ] \
  || fail "1 control: without -I the branch's argparse.py should have run at check, stop and reuse: $(cut -c1-300 "$MARK" 2>/dev/null)"
rm -f "$MARK"
pass "1 control: without -I the branch's module runs at check, stop and reuse"

# ── 2  inline merge: not yet decided, blocked ─────────────────────────────────
section2() {  # $1: isolated or control
  new_sandbox; export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  pr2_gate_setup; [ "$1" = isolated ] || strip_isolation "$SCRIPTS_DIR"
  pr2_checks none; pr2_head_age 60
  review
  [ "$LAST_RC" = 2 ] || fail "2 $1: not yet ended $LAST_RC, wanted 2: $LAST_STDERR"
  [ "$(record | jq -r '.verdict + " " + (.merge_gate_wait | tostring)')" = 'APPROVE true' ] || fail "2 $1: no gate-wait approval recorded: $(record)"
  if [ "$1" = isolated ]; then marker_empty "2 not yet (stop --merge-gate-wait)"
  else [ "$(calls ' stop .*--merge-gate-wait')" = 1 ] || fail "2 control: without -I the branch's argparse.py should have run at stop --merge-gate-wait"; rm -f "$MARK"; fi
  new_sandbox; export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  pr2_gate_setup; [ "$1" = isolated ] || strip_isolation "$SCRIPTS_DIR"
  pr2_checks red
  review
  [ "$LAST_RC" = 25 ] || fail "2 $1: blocked ended $LAST_RC, wanted 25: $LAST_STDERR"
  [ "$(record | jq -r '.verdict + " " + ((.merge_gate_wait // false) | tostring)')" = 'APPROVE false' ] || fail "2 $1: no approval recorded for the blocked gate: $(record)"
  if [ "$1" = isolated ]; then marker_empty "2 blocked (stop)"
  else [ "$(calls ' stop ')" = 1 ] || fail "2 control: without -I the branch's argparse.py should have run at the blocked gate's stop"; rm -f "$MARK"; fi
}
section2 isolated
pass "2 the inline merge's not-yet and blocked records import nothing from the branch"
section2 control
pass "2 control: without -I the branch's module runs at both records"

# ── 3  bounded tick: check, workspace ─────────────────────────────────────────
teardown || true
T="$TMP/tick"; mkdir -p "$T/scripts"
cp "$SCRIPTS/bureau-tick.sh" "$SCRIPTS/bureau-supervision.py" "$T/scripts/"
git -C "$T" init -q
cat > "$T/scripts/bureau-config.sh" <<'STUB'
BUREAU_ENV_FILE=/nonexistent
# bureau-env.sh's helper, which bureau-tick.sh calls for its merge flags (the stub replaces the
# whole config, and with it the source of bureau-env.sh).
bureau_env_caller_export() { export "$@"; }
bureau_is_paused() { return 1; }
precondition_linear() { return 0; }
agent_enabled() { return 0; }
pipeline_pick_next() { echo T-1; }
get_issue_state() { echo 'Build Review'; }
get_issue_branch() { echo 001-task; }
get_issue_detail() { jq -n '{identifier:"T-1",title:"Task",description:"Work",labels:[]}'; }
bureau_get() { echo needs-human; }
session_throttle_guard() { return 0; }
STUB
printf '#!/bin/bash\necho "$*" >> calls\nexit 0\n' > "$T/scripts/bureau-worker.sh"
shadow "$T"
tick() {
  rm -f "$T/calls"
  if (cd "$T" && PYTHONPATH=":" bash scripts/bureau-tick.sh --stage code_review > "$TMP/tick.out" 2>&1); then TICK_RC=0; else TICK_RC=$?; fi
  [ "$TICK_RC" = 0 ] || fail "3 $1: the tick ended $TICK_RC: $(cat "$TMP/tick.out")"
  grep -qxF "T-1 code-review-pipeline.sh $T/.worktrees/tick-code_review-T-1 001-task" "$T/calls" 2>/dev/null \
    || fail "3 $1: the worker did not get the workspace bureau-supervision.py names: $(cat "$T/calls" 2>/dev/null)"
}
rm -f "$MARK"
tick isolated
marker_empty "3 (check, workspace)"
pass "3 the bounded tick's check and workspace import nothing from the checkout"
strip_isolation "$T/scripts"
tick control
[ "$(calls ' check ')" = 1 ] && [ "$(calls ' workspace ')" = 1 ] \
  || fail "3 control: without -I the checkout's argparse.py should have run at check and workspace: $(cut -c1-300 "$MARK" 2>/dev/null)"
pass "3 control: without -I the checkout's module runs at check and workspace"

# ── 4  app actions and the status report ─────────────────────────────────────
# bureau-app.sh doctor, status and setup start bureau-doctor.py and bureau-runtime.py, and
# bureau-status.sh --config starts bureau-provider.py --describe for each enabled stage; the app
# runs them in the checkout it works in.
A="$TMP/app"; mkdir -p "$A"; cp -R "$SCRIPTS" "$A/scripts"; git -C "$A" init -q
jq -n '{linear:{teams:[{id:"t",key:"T",name:"Test",states:{build:"b",build_review:"br",merge:"m",done:"d"}}],labels:{lane2:{id:"l",name:"lane-2"}}},agents:{code_review:true},repo:{}}' > "$A/.bureau.json"
shadow "$A"
app() {  # $1: label
  local action out
  for action in doctor status setup; do
    out=$(cd "$A" && PYTHONPATH=":" env -u BUREAU_CONFIG bash scripts/bureau-app.sh "$action" 2>/dev/null) || true
    [ "$(printf '%s' "$out" | jq -r .workspace 2>/dev/null)" = "$A" ] || fail "4 $1: bureau-app.sh $action did not answer for the checkout: ${out:0:300}"
  done
  out=$(cd "$A" && PYTHONPATH=":" env -u BUREAU_CONFIG /bin/bash scripts/bureau-status.sh --config 2>&1 | sed 's/\x1b\[[0-9;]*m//g') || true
  grep -qx '    code_review: claude / CLI default / read-only' <<< "$out" || fail "4 $1: bureau-status.sh --config lacks the provider row: $(printf '%s' "$out" | grep -A2 PROVIDERS)"
}
rm -f "$MARK"
app isolated
marker_empty "4 (bureau-app.sh doctor, status, setup; bureau-status.sh --config)"
pass "4 the app actions and the status report import nothing from the checkout"
sed -i.bak -E 's/python3 -I ("\$SCRIPT_DIR\/bureau-)/python3 \1/' "$A/scripts/bureau-app.sh"
sed -i.bak -E 's/python3 -I ("\$\(dirname "\$BUREAU_RUNTIME"\)\/bureau-provider\.py")/python3 \1/' "$A/scripts/bureau-status.sh"
rm -f "$A"/scripts/*.bak
! grep -qE 'python3 -I' "$A/scripts/bureau-app.sh" "$A/scripts/bureau-status.sh" || { echo "control: -I is still in the app or status script" >&2; exit 1; }
app control
for call in 'bureau-doctor\.py' 'bureau-runtime\.py status' 'bureau-runtime\.py setup' 'bureau-provider\.py .*--describe'; do
  grep -qE "^argparse .*$call" "$MARK" 2>/dev/null || fail "4 control: without -I the checkout's argparse.py should have run at $call"
done
pass "4 control: without -I the checkout's module runs at doctor, status, setup and --describe"

if [ "$FAILS" != 0 ]; then echo "$FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_supervision_isolated"
