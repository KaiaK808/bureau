#!/bin/bash
# Real QA stage and provider; only the model and external APIs are fake.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
BASE_PATH="$PATH"

setup() {
  local runner="${1:-codex}" test_exit="${2:-0}" notes="${3:-SANDBOX_GATE: socket test cannot bind inside the sandbox}"
  export PATH="$BASE_PATH"
  sandbox_init TEAM-123 test-branch
  export BUREAU_CONFIG="$SANDBOX/.bureau.json" BUREAU_STUB_RUNNER="$runner" BUREAU_RUNNER_QA="$runner"
  export BUREAU_STUB_STATE_QA=state-qa BUREAU_STUB_ISSUE_STATE=QA
  export FAKE_CLAUDE_PROMPT_LOG="$SANDBOX/logs/prompt.log" FAKE_CLAUDE_FIXTURES="$SANDBOX/logs/result.json"
  cat > "$SANDBOX/.gitignore" <<'EOF'
.env
.bureau.json
.fake-origin.git/
scripts/
logs/
*.log
fake-bin/
fake_claude_counter
EOF
  jq -n --arg runner "$runner" --arg test "printf 'run\\n' >> logs/test-runs; exit $test_exit" \
    '{agents:{runner:$runner},repo:{test_command:$test}}' > "$BUREAU_CONFIG"
  jq -n --arg notes "$notes" '{status:"NEEDS_HUMAN",tests_added:0,tests_failing:0,coverage_notes:$notes}' > "$FAKE_CLAUDE_FIXTURES"
  # A committed tasks.md keeps the green Phase 1 case on the coverage path.
  git -C "$SANDBOX" add .gitignore specs
  git -C "$SANDBOX" commit -qm 'test: baseline with task coverage to check'
  git -C "$SANDBOX" push -q origin HEAD
  if [ "$runner" = codex ]; then
    # The real adapter entry, auth and publication helpers, as in the stage publication test.
    sed -n '/^run_stage_for() {/,/^# Build the model invocation/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
    cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
BUREAU_RUNTIME="$(dirname "$0")/bureau-runtime.py"
EOF
    mkdir "$SANDBOX/fake-bin"
    cat > "$SANDBOX/fake-bin/codex" <<'EOF'
#!/usr/bin/env python3
import json, pathlib, sys
if sys.argv[1]=='login': sys.exit(0)
assert sys.argv[1]=='exec'
prompt=sys.stdin.read()
assert 'Do not run git commit or git push.' in prompt
assert '--output-schema' in sys.argv
pathlib.Path('logs/prompt.log').write_text(prompt)
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(pathlib.Path('logs/result.json').read_text())
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}))
EOF
    chmod +x "$SANDBOX/fake-bin/codex"
    export PATH="$SANDBOX/fake-bin:$PATH"
  else
    # Keep the harness's Claude stub; only publication runs through the real helper.
    sed -n '/^commit_stage_changes() {/,/^# Build the model invocation/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  fi
}

test_runs() {
  if [ -f "$SANDBOX/logs/test-runs" ]; then wc -l < "$SANDBOX/logs/test-runs" | tr -d ' '; else echo 0; fi
}

assert_green() {
  assert_eq 0 "$LAST_RC" 'green QA gate exit' || return 1
  assert_calls_include 'move_issue.*state-build-review' 'green QA hands off' || return 1
  assert_calls_exclude 'move_issue.*state-build$' 'green QA does not request rework' || return 1
  assert_calls_exclude 'add_issue_label.*needs-human' 'green QA has no human label' || return 1
  assert_eq 2 "$(test_runs)" 'initial and final outside runs, no extra gate run' || return 1
}

assert_halted() {
  assert_eq 0 "$LAST_RC" 'QA keeps its existing labelled NEEDS_HUMAN exit' || return 1
  assert_calls_include 'add_issue_label.*needs-human' 'human label' || return 1
  assert_calls_exclude 'move_issue' 'no hand-off' || return 1
  assert_match 'Status: NEEDS_HUMAN' "$LAST_STDOUT" 'halt status' || return 1
}

assert_decision_first() {
  assert_match "$1" "$LAST_STDOUT" 'decision echoed'
  assert_calls_include "$1" 'decision in Linear comment'
  python3 - "$SANDBOX/calls.log" "$1" <<'PY'
from pathlib import Path
import sys
text=Path(sys.argv[1]).read_text(); line=sys.argv[2]
assert text.count(line)==1, 'the decision must appear once in the comment'
assert '\n\n'+line+'\n\nSANDBOX_GATE:' in text, 'the decision must precede the coverage notes'
PY
}

setup
run_pipeline qa-pipeline.sh TEAM-123
assert_green
assert_file_contains 'Sandbox: this turn runs inside the Codex sandbox' "$SANDBOX/logs/prompt.log" 'Codex sandbox note'
assert_file_contains 'coverage_notes beginning SANDBOX_GATE:' "$SANDBOX/logs/prompt.log" 'QA result contract'
assert_decision_first 'shell gate outside the Codex sandbox green: QA continues as GREEN'
teardown

# The command fails in Phase 1, its retry and Phase 3 alike.
setup codex 1
run_pipeline qa-pipeline.sh TEAM-123
assert_eq 0 "$LAST_RC" 'red QA keeps the existing rework exit'
assert_calls_include 'move_issue.*state-build$' 'red QA returns to Build'
assert_calls_exclude 'move_issue.*state-build-review' 'red QA does not hand off'
assert_calls_exclude 'add_issue_label.*needs-human' 'red QA has no human label'
assert_eq 3 "$(test_runs)" 'both initial attempts and the final run fail outside'
assert_decision_first 'shell gate outside the Codex sandbox red: QA continues as RED'
teardown

setup codex 0 'A correctness bug needs a product decision'
run_pipeline qa-pipeline.sh TEAM-123
assert_halted
assert_eq 2 "$(test_runs)" 'passing suite does not override a correctness blocker'
assert_calls_exclude 'shell gate outside the Codex sandbox' 'no sandbox decision without prefix'
teardown

setup claude
run_pipeline qa-pipeline.sh TEAM-123
assert_halted
assert_eq 2 "$(test_runs)" 'Claude keeps its initial and final test runs'
assert_calls_exclude 'shell gate outside the Codex sandbox' 'Claude has no sandbox decision'
if grep -qF 'Sandbox: this turn runs inside the Codex sandbox' "$SANDBOX/logs/prompt.log"; then
  echo 'FAIL: Claude prompt received the Codex sandbox note' >&2; exit 1
fi
teardown

# A denied-operation phrase must survive the real provider instead of exit 24.
setup codex 0 'SANDBOX_GATE: socket test: Operation not permitted on bind'
run_pipeline qa-pipeline.sh TEAM-123
assert_green
assert_decision_first 'shell gate outside the Codex sandbox green: QA continues as GREEN'
teardown

# Negative controls use the same cases with one production decision removed.
setup
python3 - "$SCRIPTS_DIR/qa-pipeline.sh" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]); text=path.read_text()
start='# The shell gate after a Codex QA turn.'
end="# Claude's self-reported status and the objective suite result must agree;"
assert text.count(start)==1 and text.count(end)==1
block=text[text.index(start):text.index(end)]
assert text.count(block)==1
path.write_text(text.replace(block,''))
PY
run_pipeline qa-pipeline.sh TEAM-123
if assert_green > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: removing the QA decision passed the green-case assertion' >&2; exit 1
fi
assert_halted
assert_eq 2 "$(test_runs)" 'without the decision the passing suite still leaves needs-human'
teardown

setup codex 0 'SANDBOX_GATE: socket test: Operation not permitted on bind'
python3 - "$SCRIPTS_DIR/bureau-provider.py" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]); text=path.read_text()
exception="    if runner == 'codex' and stage == 'qa' and qa_sandbox_gate_only(value): return False\n"
assert text.count(exception)==1
path.write_text(text.replace(exception,''))
PY
run_pipeline qa-pipeline.sh TEAM-123
assert_eq 24 "$LAST_RC" 'without the provider exception QA aborts on the denied operation'
assert_eq 1 "$(test_runs)" 'provider refusal stops before Phase 3'
assert_calls_exclude 'move_issue' 'provider refusal does not route'
assert_calls_exclude 'add_issue_label.*needs-human' 'provider refusal stops before routing'
teardown
trap - EXIT
echo 'OK test_codex_qa_sandbox_gate'
