#!/bin/bash
# agents.merge_mode: "auto" (default) lets the pipeline merge; "manual" means a human does.
#
#   1. The real config reads the key; absent/null is auto, every other value but
#      "auto"/"manual" falls closed to manual with a warning — and bureau-doctor.py
#      reports the same mode for the same value.
#   2. rebase-pipeline.sh under manual refuses with exit 2 before Linear, gh or git,
#      for the queue and a named ticket; under auto it reaches Linear (negative control).
#   3. The review stage under manual never merges: with a Merge state an APPROVE parks
#      the ticket there (exit 0), also under a requested stop; without one the stage
#      refuses at its start with 24, before any helper, paid review or gh call. Under
#      auto the same APPROVE runs the inline merge (negative control).
#   4. The queue loop's real run_script: merge and rebase are off under manual (no
#      picker call), and only a 20 the caller asked for (--no-merge) is quiet.
#
# merge-pipeline.sh is covered against the real config in
# tests/test_merge_pipeline_correctness.sh, the shepherd in tests/test_shepherd.sh.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

set_mode() {  # $1 = JSON value for .agents.merge_mode, or "absent"
  [ -f "$SANDBOX/.bureau.json" ] || echo '{}' > "$SANDBOX/.bureau.json"  # none reads as {}
  if [ "$1" = absent ]; then
    jq 'del(.agents.merge_mode)' "$SANDBOX/.bureau.json" > "$SANDBOX/.bureau.json.tmp"
  else
    jq --argjson v "$1" '.agents.merge_mode = $v' "$SANDBOX/.bureau.json" > "$SANDBOX/.bureau.json.tmp"
  fi
  mv "$SANDBOX/.bureau.json.tmp" "$SANDBOX/.bureau.json"
}

# ── 1. The real config and the doctor agree ─────────────────────────────
REAL_CONFIG="$REPO_ROOT/templates/scripts/bureau-config.sh"
CFG_DIR=$(mktemp -d -t bureau-test.mode.XXXXXXXX)
trap 'rm -rf "$CFG_DIR"; teardown || true' EXIT
git -C "$CFG_DIR" init -q
check_mode() {  # $1 = JSON value or absent, $2 = expected mode, $3 = warn|quiet
  if [ "$1" = absent ]; then echo '{"linear":{"teams":[{}]},"agents":{}}' > "$CFG_DIR/.bureau.json"
  else jq -n --argjson v "$1" '{linear:{teams:[{}]},agents:{merge_mode:$v}}' > "$CFG_DIR/.bureau.json"; fi
  local out err
  out=$(cd "$CFG_DIR" && BUREAU_CONFIG="$CFG_DIR/.bureau.json" /bin/bash -c 'source "$1"; printf "%s" "$BUREAU_MERGE_MODE"; if bureau_merge_is_manual; then printf " manual-predicate"; fi' _ "$REAL_CONFIG" 2>"$CFG_DIR/err")
  err=$(cat "$CFG_DIR/err")
  local want="$2"; [ "$2" = manual ] && want="manual manual-predicate"
  assert_eq "$want" "$out" "shell mode for $1"
  if [ "$3" = warn ]; then assert_match "merge_mode = .* falling closed to manual" "$err" "warning for $1"
  elif grep -q 'merge_mode' <<< "$err"; then echo "FAIL: warning for valid value $1: $err" >&2; exit 1; fi
  local doctor
  doctor=$(python3 -c 'import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("d", sys.argv[1]); d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
print(d.merge_mode(json.load(open(sys.argv[2])))[0])' "$REPO_ROOT/templates/scripts/bureau-doctor.py" "$CFG_DIR/.bureau.json")
  assert_eq "$2" "$doctor" "doctor mode for $1"
}
check_mode absent       auto   quiet
check_mode null         auto   quiet
check_mode '"auto"'     auto   quiet
check_mode '"manual"'   manual quiet
check_mode '"Manual"'   manual warn
check_mode '"off"'      manual warn
check_mode 'false'      manual warn
check_mode 'true'       manual warn
check_mode '0'          manual warn
check_mode '{"x":1}'    manual warn
check_mode '"auto\n"'   manual warn
check_mode '"manual\n"' manual warn
check_mode '" auto"'    manual warn
# The environment cannot switch a manual repo to auto.
jq -n '{linear:{teams:[{}]},agents:{merge_mode:"manual"}}' > "$CFG_DIR/.bureau.json"
out=$(cd "$CFG_DIR" && BUREAU_MERGE_MODE=auto BUREAU_CONFIG="$CFG_DIR/.bureau.json" /bin/bash -c 'source "$1"; if bureau_merge_is_manual; then echo manual; fi' _ "$REAL_CONFIG" 2>/dev/null)
assert_eq manual "$out" "environment override ignored"

# ── 2. rebase-pipeline.sh ───────────────────────────────────────────────
sandbox_init EXP-700 test-branch
export BUREAU_STUB_STATE_MERGE=state-merge GH_STUB_EXISTING_PR=99
for args in "" EXP-700; do
  set_mode '"manual"'
  rm -f "$SANDBOX/gh_calls.log"
  # shellcheck disable=SC2086
  run_pipeline rebase-pipeline.sh $args
  assert_eq 2 "$LAST_RC" "rebase refuses under manual (${args:-queue})"
  assert_match 'merge_mode is manual' "$LAST_STDOUT" "rebase names the reason"
  if [ -s "$SANDBOX/calls.log" ]; then echo "FAIL: rebase under manual called a helper:" >&2; cat "$SANDBOX/calls.log" >&2; exit 1; fi
  if [ -s "$SANDBOX/gh_calls.log" ]; then echo "FAIL: rebase under manual called gh" >&2; exit 1; fi
  # Negative control: under auto the same call reaches Linear and gh.
  set_mode '"auto"'
  run_pipeline rebase-pipeline.sh $args
  assert_calls_include 'precondition_linear' "rebase under auto reaches Linear (${args:-queue})"
  assert_file_contains 'pr' "$SANDBOX/gh_calls.log" "rebase under auto reaches gh"
done
teardown

# ── 3. The review stage's APPROVE arm ───────────────────────────────────
approve_sandbox() {
  sandbox_init EXP-701 test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  cat > "$SANDBOX/approve.txt" <<'EOF'
Review checked.
```json
{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"Checks passed"}
```
EOF
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt"
  export BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  unset BUREAU_CALLER_STOP
}
claude_runs() { cat "$SANDBOX/fake_claude_counter" 2>/dev/null || echo 0; }
entered_merge() { grep -q 'Merge Pipeline:' <<< "$LAST_STDOUT" || grep -q $'pr\tmerge' "$SANDBOX/gh_calls.log" 2>/dev/null; }

# 3a. manual with a Merge state: parked in Merge, no merge, exit 0.
approve_sandbox
export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=''
set_mode '"manual"'
run_pipeline code-review-pipeline.sh EXP-701
[ "$LAST_RC" = 0 ] || printf '%s\n%s\n' "$LAST_STDOUT" "$LAST_STDERR"
assert_eq 0 "$LAST_RC" "manual + Merge state: review ends cleanly"
assert_calls_include $'move_issue\tEXP-701\tstate-merge' "manual + Merge state: parked in Merge"
assert_calls_include 'awaits a manual merge' "manual + Merge state: says a human merges"
if entered_merge; then echo "FAIL: manual + Merge state entered the merge" >&2; exit 1; fi
# Also with the merge agent on (merge_mode wins over agents.merge).
export BUREAU_STUB_AGENT_ENABLED=merge
run_pipeline code-review-pipeline.sh EXP-701
assert_calls_include 'awaits a manual merge' "manual wins over agents.merge"
teardown

# 3b. manual under a requested stop (--no-merge) still parks in Merge: moving is not
#     merging, and the Merge state is the manual mode's stopping place.
approve_sandbox
export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED='' BUREAU_NO_MERGE=1
set_mode '"manual"'
run_pipeline code-review-pipeline.sh EXP-701
assert_eq 0 "$LAST_RC" "manual + --no-merge: parked, not stopped"
assert_calls_include $'move_issue\tEXP-701\tstate-merge' "manual + --no-merge: parked in Merge"
if entered_merge; then echo "FAIL: manual + --no-merge entered the merge" >&2; exit 1; fi
export BUREAU_NO_MERGE=0
teardown

# 3c. manual without a Merge state: the stage refuses at its start with 24 and a reason
#     naming the key — no helper call (Linear), no paid review, no gh call, no record.
for value in '"manual"' '"Manual"'; do
  approve_sandbox
  export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  set_mode "$value"
  run_pipeline code-review-pipeline.sh EXP-701
  assert_eq 24 "$LAST_RC" "manual without Merge state ($value): refused"
  assert_match 'linear.teams\[0\].states.merge is not set' "$LAST_STDERR" "refusal names the key ($value)"
  if [ -s "$SANDBOX/calls.log" ]; then echo "FAIL: refusal ($value) called a helper:" >&2; cat "$SANDBOX/calls.log" >&2; exit 1; fi
  assert_eq 0 "$(claude_runs)" "refusal ($value): nothing paid"
  if [ -s "$SANDBOX/gh_calls.log" ]; then echo "FAIL: refusal ($value) called gh" >&2; exit 1; fi
  if [ -e "$SANDBOX/.git/bureau/review-stops.json" ]; then echo "FAIL: refusal ($value) wrote a review boundary" >&2; exit 1; fi
  teardown
done

# 3d. Negative control: the same APPROVE under auto, without a Merge state, runs the
#     review and the inline merge.
for value in absent '"auto"'; do
  approve_sandbox
  export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  set_mode "$value"
  run_pipeline code-review-pipeline.sh EXP-701
  [ "$(claude_runs)" -gt 0 ] || { echo "FAIL: negative control: auto ($value) paid no review" >&2; exit 1; }
  if ! entered_merge; then
    printf '%s\n%s\n' "$LAST_STDOUT" "$LAST_STDERR" >&2
    echo "FAIL: negative control: auto ($value) no longer reaches the inline merge, so this proves nothing" >&2; exit 1
  fi
  teardown
done

# ── 4. The queue loop ───────────────────────────────────────────────────
# The real run_script, cut from queue-loop.sh, with a stub worker. A 20 the caller asked
# for (BUREAU_NO_MERGE) is logged quietly; any other 20 alerts, under manual too (no
# template stage exits 20 there). Merge and rebase are off under manual: run_script
# returns 2 before the picker.
Q=$(mktemp -d -t bureau-test.queue.XXXXXXXX)
trap 'rm -rf "$CFG_DIR" "$Q"; teardown || true' EXIT
mkdir -p "$Q/scripts"
printf '#!/bin/bash\nexit "${STUB_RC:-20}"\n' > "$Q/scripts/bureau-worker.sh"
{
  echo 'bureau_get() { jq -r "$1" "$Q/.bureau.json"; }'
  sed -n '/^# Capture the caller boundary/,/^BUREAU_RUNTIME=/{ /^BUREAU_RUNTIME=/d; p; }' "$REAL_CONFIG"
  sed -n '/^# ── Merge policy (agents.merge_mode)/,/^# ── End of merge policy/p' "$REAL_CONFIG"
  sed -n '/^exit_class() {/,/^}/p' "$REAL_CONFIG"
  sed -n '/^run_script() {/,/^}/p' "$REPO_ROOT/templates/scripts/queue-loop.sh"
} > "$Q/queue.sh"
grep -q '^run_script() {' "$Q/queue.sh" || { echo "FAIL: run_script not found in queue-loop.sh" >&2; exit 1; }
queue_run() {  # $1 = merge_mode JSON value, $2 = BUREAU_NO_MERGE, $3 = stage exit code; prints alert count
  jq -n --argjson v "$1" '{agents:{merge_mode:$v}}' > "$Q/.bureau.json"
  rm -f "$Q/alerts" "$Q/log" "$Q/picks"
  Q="$Q" BUREAU_NO_MERGE="$2" STUB_RC="${3:-20}" SCRIPT="${4:-}" /bin/bash -c '
    source "$Q/queue.sh"
    REPO_DIR="$Q"; LOG_FILE="$Q/log"; MODE=all
    preselect_issue() { echo pick >> "$Q/picks"; echo EXP-9; }
    get_issue_branch() { echo feat/x; }
    emit_event() { :; }
    session_throttle_guard() { return 0; }
    alert_telegram() { echo "$4" >> "$Q/alerts"; }
    run_script "${SCRIPT:-code-review-pipeline.sh}" "Stage" "$Q"
    echo "rc=$?" >> "$Q/log"' >/dev/null 2>&1 || true
  [ -n "${4:-}" ] || grep -q "^rc=${3:-20}\$" "$Q/log" || { echo "FAIL: run_script did not return the stage code:" >&2; cat "$Q/log" >&2; exit 1; }
  grep -c . "$Q/alerts" 2>/dev/null || echo 0
}
assert_eq 0 "$(queue_run '"auto"' 1)" "queue: --no-merge stop before merge is quiet"
assert_match 'stopped before merge as asked' "$(cat "$Q/log")" "queue: the quiet stop is logged"
# Negative controls: an unasked 20 alerts, under auto and under manual; so does a BLOCK.
assert_eq 1 "$(queue_run '"auto"' 0)" "queue: an unasked 20 alerts"
assert_eq 1 "$(queue_run '"manual"' 0)" "queue: a 20 under manual alerts"
assert_eq 1 "$(queue_run '"manual"' 0 25)" "queue: a BLOCK under manual alerts"
assert_eq 1 "$(queue_run '"auto"' 1 25)" "queue: a BLOCK under --no-merge alerts"
# Merge and rebase under manual: off before the picker, quiet, return 2.
for script in merge-pipeline.sh rebase-pipeline.sh; do
  assert_eq 0 "$(queue_run '"manual"' 0 0 "$script")" "queue: $script under manual is quiet"
  assert_match '^rc=2$' "$(cat "$Q/log")" "queue: $script under manual returns 2"
  assert_match 'off: agents.merge_mode is manual' "$(cat "$Q/log")" "queue: $script says why"
  if [ -e "$Q/picks" ]; then echo "FAIL: queue: $script under manual called the picker" >&2; exit 1; fi
  # Negative control: under auto the same explicit mode picks.
  queue_run '"auto"' 0 0 "$script" >/dev/null
  [ -s "$Q/picks" ] || { echo "FAIL: negative control: $script under auto did not pick" >&2; exit 1; }
done

# ── 5. bureau-status.sh --config shows the mode in effect and its source ──
S=$(mktemp -d -t bureau-test.status.XXXXXXXX)
trap 'rm -rf "$CFG_DIR" "$Q" "$S"; teardown || true' EXIT
cp -R "$REPO_ROOT/templates/scripts" "$S/scripts"; git -C "$S" init -q
status_rows() {  # $1 = jq edit on the base config; prints the merge_mode, merge and rebase rows
  jq -n '{linear:{teams:[{id:"t",key:"T",name:"Test",states:{build:"b",build_review:"br",merge:"m",done:"d"}}],labels:{lane2:{id:"l",name:"lane-2"}}},agents:{merge:true,rebase:true},repo:{}}' \
    | jq "$1" > "$S/.bureau.json"
  (cd "$S" && env -u BUREAU_CONFIG /bin/bash scripts/bureau-status.sh --config 2>/dev/null) \
    | sed 's/\x1b\[[0-9;]*m//g' | grep -E '^ +(agents\.merge_mode|merge|rebase) ' | tr -s ' '
}
rows=$(status_rows '.')
assert_match '^ agents.merge_mode def auto$' "$rows" "status: absent key is the default"
assert_match '^ merge json ON$' "$rows" "status: merge agent on under auto"
rows=$(status_rows '.agents.merge_mode = "manual"')
assert_match '^ agents.merge_mode json manual$' "$rows" "status: manual from json"
assert_match '^ merge json refused: merge_mode manual$' "$rows" "status: merge refused under manual"
assert_match '^ rebase json refused: merge_mode manual$' "$rows" "status: rebase refused under manual"
rows=$(status_rows '.agents.merge_mode = "Manual"')
assert_match '^ agents.merge_mode json manual \(fell closed from "Manual"\)$' "$rows" "status: an invalid value says it fell closed"
rows=$(status_rows '.agents.merge_mode = "manual" | del(.linear.teams[0].states.merge)')
assert_match 'no Merge state: code review refuses \(24\)$' "$rows" "status: manual without Merge state"

echo 'OK test_merge_mode'
