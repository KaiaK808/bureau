#!/bin/bash
# Real QA stage and provider; only the model and external APIs are fake.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
BASE_PATH="$PATH"

setup() {
  local runner="${1:-codex}" test_exit="${2:-0}" notes="${3:-SANDBOX_GATE: socket test cannot bind inside the sandbox}"
  unset FAKE_GATE_MODE FAKE_GATE_COMMAND
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
  jq -n --arg runner "$runner" --arg test "printf 'run\\n' >> logs/test-runs; git rev-parse HEAD >> logs/test-heads; exit $test_exit" \
    '{agents:{runner:$runner},repo:{test_command:$test}}' > "$BUREAU_CONFIG"
  jq -n --arg notes "$notes" '{status:"NEEDS_HUMAN",tests_added:1,tests_failing:0,coverage_notes:$notes}' > "$FAKE_CLAUDE_FIXTURES"
  # A committed tasks.md keeps the green Phase 1 case on the coverage path.
  git -C "$SANDBOX" add .gitignore specs
  git -C "$SANDBOX" commit -qm 'test: baseline with task coverage to check'
  git -C "$SANDBOX" push -q origin HEAD
  if [ "$runner" = codex ]; then
    export CODEX_HOME="$SANDBOX/logs/codex-home" FAKE_GATE_MODE=abort
    # The real adapter entry, auth and publication helpers, as in the stage publication test.
    sed -n '/^run_stage_for() {/,/^# Build the model invocation/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
    cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
BUREAU_RUNTIME="$(dirname "$0")/bureau-runtime.py"
EOF
    mkdir "$SANDBOX/fake-bin"
    cp "$REPO_ROOT/tests/lib/codex_gate_events.py" "$SANDBOX/fake-bin/gate_events.py"
    cat > "$SANDBOX/fake-bin/codex" <<'EOF'
#!/usr/bin/env python3
import json, os, pathlib, runpy, sys
if sys.argv[1]=='login': sys.exit(0)
assert sys.argv[1]=='exec'
prompt=sys.stdin.read()
assert 'Do not run git commit or git push.' in prompt
assert '--output-schema' in sys.argv
pathlib.Path('logs/prompt.log').write_text(prompt)
if not os.environ.get('FAKE_CODEX_NO_CHANGE'):
    pathlib.Path('tests').mkdir(exist_ok=True)
    pathlib.Path('tests/test_added.py').write_text('import unittest\nclass Added(unittest.TestCase):\n    def test_add(self): self.assertEqual(2 + 3, 5)\n')
if os.environ.get('FAKE_GATE_MODE'):
    runpy.run_path('fake-bin/gate_events.py')['emit']()
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

qa_log_path() {
  local log
  for log in "$SANDBOX"/logs/qa-*.log; do
    [ -f "$log" ] || continue
    printf '%s' "$log"
    return 0
  done
  return 1
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

machine_setup() {
  setup codex "${2:-0}" 'unprefixed configured gate aborted'
  export FAKE_GATE_MODE="${1:-abort}"
  jq --arg cmd "$(jq -r '.repo.test_command' "$BUREAU_CONFIG")" \
    '.blockers=[{kind:"gate",command:$cmd,reason:"gate aborted"}]' \
    "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/typed.json"
  mv "$SANDBOX/logs/typed.json" "$FAKE_CLAUDE_FIXTURES"
}

machine_teardown() { unset FAKE_GATE_MODE; teardown; }

assert_publication() {
  python3 "$REPO_ROOT/tests/lib/assert_gate_publication.py" "$SANDBOX" qa
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
assert_publication
assert_file_contains 'Sandbox: this turn runs inside the Codex sandbox' "$SANDBOX/logs/prompt.log" 'Codex sandbox note'
assert_file_contains 'coverage_notes beginning SANDBOX_GATE:' "$SANDBOX/logs/prompt.log" 'QA result contract'
assert_file_contains 'Optional insights: normally use an empty array' "$SANDBOX/logs/prompt.log" 'QA insight prompt'
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
assert_publication
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

# The squash-range check still holds the ticket after the gate decided GREEN:
# a CI suppressor (the first entry of the marker list) in an earlier commit.
setup
printf 'x\n' > "$SANDBOX/earlier.txt"
git -C "$SANDBOX" add earlier.txt
git -C "$SANDBOX" commit -qm "TEAM-123: earlier work $(head -n 1 "$REPO_ROOT/templates/scripts/ci-skip-markers.txt")"
git -C "$SANDBOX" push -q origin HEAD
run_pipeline qa-pipeline.sh TEAM-123
assert_halted
assert_match 'shell gate outside the Codex sandbox green: QA continues as GREEN' "$LAST_STDOUT" 'the gate decided before the squash check'
assert_eq 2 "$(test_runs)" 'the squash hold does not rerun the suite'
teardown

for mode in abort kill red; do
  machine_setup "$mode"
  run_pipeline qa-pipeline.sh TEAM-123
  assert_green
  assert_publication
  assert_calls_include 'shell gate outside the Codex sandbox green' 'unprefixed machine gate decides'
  assert_file_contains 'Outside gate log:.*logs/outside-gates/' "$(qa_log_path)" 'outside log attached'
  machine_teardown
done

machine_setup red 1
run_pipeline qa-pipeline.sh TEAM-123
assert_eq 0 "$LAST_RC" 'outside red keeps rework exit'
assert_calls_include 'move_issue.*state-build$' 'outside red stays blocked from review'
assert_calls_exclude 'move_issue.*state-build-review' 'outside red never hands off'
assert_eq 3 "$(test_runs)" 'red QA has two initial runs and one final rerun'
assert_file_contains 'Outside gate log:.*logs/outside-gates/' "$(qa_log_path)" 'outside red log attached'
assert_publication
machine_teardown

# The normal final outside run still passes at B without a link, but cannot
# clear the hold taken at A. The genuine-edit green assertion must reject it.
machine_setup red
python3 - "$SCRIPTS_DIR/bureau-evidence.py" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text()
anchor="        receipt_head = value['end_head']\n"
assert text.count(anchor)==1
p.write_text(text.replace(anchor, anchor+"        if checked_head != receipt_head: return None\n"))
PY
run_pipeline qa-pipeline.sh TEAM-123
if assert_green > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: dropping the publication link passed the genuine-edit QA assertion' >&2; exit 1
fi
assert_halted
assert_eq 2 "$(test_runs)" 'no publication link retains only normal QA runs'
echo 'PASS negative control: dropping the publication link fails genuine-edit QA handoff'
machine_teardown

for mode in stale unknown missing_cwd borrowed_cwd duplicate_begin duplicate_end reordered ambiguous wrong_end agent_bash relative_bash env_agent_bash mixed; do
  machine_setup "$mode"
  if [ "$mode" = mixed ]; then
    jq '.blockers += [{kind:"product",command:"",reason:"Product decision remains"}]' "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/mixed.json"
    mv "$SANDBOX/logs/mixed.json" "$FAKE_CLAUDE_FIXTURES"
  fi
  run_pipeline qa-pipeline.sh TEAM-123
  assert_halted
  assert_eq 2 "$(test_runs)" "$mode retains only QA's normal initial and final tests"
  machine_teardown
done

# Negative control: wrongly allow green to erase a product blocker. The SAME
# mixed-case assertions must reject that mutation.
machine_setup red
jq '.status="GREEN" | .blockers += [{kind:"product",command:"",reason:"Product decision remains"}]' "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/contradictory.json"
mv "$SANDBOX/logs/contradictory.json" "$FAKE_CLAUDE_FIXTURES"
run_pipeline qa-pipeline.sh TEAM-123
assert_halted
assert_eq 2 "$(test_runs)" 'a claimed GREEN cannot erase an explicit product hold'
machine_teardown

machine_setup red
jq '.blockers += [{kind:"product",command:"",reason:"Product decision remains"}]' "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/mixed.json"
mv "$SANDBOX/logs/mixed.json" "$FAKE_CLAUDE_FIXTURES"
python3 - "$SCRIPTS_DIR/qa-pipeline.sh" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text(); needle='# Claude\'s self-reported status'
assert text.count(needle)==1
p.write_text(text.replace(needle,'[ "$FINAL_GREEN" != true ] || STATUS="GREEN"\n\n'+needle))
PY
run_pipeline qa-pipeline.sh TEAM-123
if assert_halted > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: product-clearing mutation passed mixed-blocker assertion' >&2; exit 1
fi
assert_green
echo 'PASS negative control: outside green clearing a product blocker fails assert_halted'
machine_teardown

# The prefix is syntax, never failed-execution proof by itself.
setup
unset FAKE_GATE_MODE
run_pipeline qa-pipeline.sh TEAM-123
assert_halted
assert_eq 2 "$(test_runs)" 'prefix without proof cannot clear a hold through normal QA tests'
teardown

setup
export FAKE_GATE_MODE=stale
run_pipeline qa-pipeline.sh TEAM-123
assert_halted
teardown

# QA consumes original stage logs; a lost original cannot become a green handoff.
machine_setup red
python3 - "$SCRIPTS_DIR/bureau-provider.py" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]);text=p.read_text();anchor='    return json.dumps(dict(eligible=verified, green=green and verified, logs=logs,'
assert text.count(anchor)==1
p.write_text(text.replace(anchor,"    for log in logs: Path(log).unlink(missing_ok=True)\n"+anchor))
PY
run_pipeline qa-pipeline.sh TEAM-123
assert_halted
assert_eq 2 "$(test_runs)" 'lost stage log never adds another run'
assert_file_contains 'Outside gate log unavailable:' "$(qa_log_path)" 'lost log is reported from the stage log pointer'
machine_teardown

# The shared deterministic QA fallback is also a configured gate identity.
setup
cat > "$SCRIPTS_DIR/bureau-test.sh" <<'EOF'
#!/bin/bash
printf 'run\n' >> logs/test-runs
EOF
jq '.repo.test_command=""' "$BUREAU_CONFIG" > "$SANDBOX/logs/config-new"
mv "$SANDBOX/logs/config-new" "$BUREAU_CONFIG"
export FAKE_GATE_COMMAND='bash scripts/bureau-test.sh'
jq --arg command "$FAKE_GATE_COMMAND" '.coverage_notes="gate aborted" | .blockers=[{kind:"gate",command:$command,reason:"gate aborted"}]' \
  "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/result-new"
mv "$SANDBOX/logs/result-new" "$FAKE_CLAUDE_FIXTURES"
run_pipeline qa-pipeline.sh TEAM-123
assert_green
python3 - "$SANDBOX" <<'PY'
import json, pathlib, sys
receipt=json.loads(next((pathlib.Path(sys.argv[1])/'.git/bureau/evidence/v1').glob('runs/*/receipts/*.json')).read_text())
assert receipt['gate_attempts'][0]['command_id']=='qa.test'
PY
unset FAKE_GATE_COMMAND
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
for stop_code in 25 124 130; do
  setup
  cat >> "$SCRIPTS_DIR/bureau-config.sh" <<EOF
codex_gate_run() {
  printf '%s\n' '{"eligible":false,"green":false,"reason":"outside gate termination uncertain","failure_cause":"human_hold","logs":["uncertain-gate.log"]}'
  return $stop_code
}
EOF
  run_pipeline qa-pipeline.sh TEAM-123
  assert_eq "$stop_code" "$LAST_RC" 'uncertain QA outside gate preserves its stop'
  assert_calls_exclude 'move_issue' 'uncertain QA gate never routes'
  assert_calls_exclude 'add_issue_label.*needs-human' 'uncertain QA gate stops before routing mutations'
  assert_match 'uncertain-gate.log' "$LAST_STDERR" 'uncertain QA stop keeps its log pointer'
  teardown
done

setup
cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
codex_gate_run() { printf '%s\n' '{"green":false,"logs":[]}'; return 25; }
EOF
python3 - "$SCRIPTS_DIR/qa-pipeline.sh" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text(); anchor='25|124|130)'
assert text.count(anchor)==1
p.write_text(text.replace(anchor,'124|130)',1))
PY
run_pipeline qa-pipeline.sh TEAM-123
if assert_eq 25 "$LAST_RC" 'uncertain QA gate stop' > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: removing the uncertain QA stop passed its exit assertion' >&2; exit 1
fi
echo 'PASS negative control: QA must propagate the uncertain gate hold'
teardown
trap - EXIT
echo 'OK test_codex_qa_sandbox_gate'
