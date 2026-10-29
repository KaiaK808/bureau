#!/bin/bash
# Real implement stage and provider; only the model and external APIs are fake.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
BASE_PATH="$PATH"

setup() {
  local runner="${1:-codex}" mixed="${2:-0}" test_exit="${3:-0}" hook="${4:-}"
  unset FAKE_GATE_MODE
  export PATH="$BASE_PATH"
  sandbox_init TEAM-123 test-branch
  export BUREAU_CONFIG="$SANDBOX/.bureau.json" BUREAU_STUB_RUNNER="$runner" BUREAU_RUNNER_IMPLEMENT="$runner"
  export BUREAU_USE_GOAL_LOOP=0 BUREAU_IMPL_MAX_ITER=1
  export FAKE_CLAUDE_COMMIT_ON_ITERS=1 FAKE_CLAUDE_PROMPT_LOG="$SANDBOX/logs/prompt.log"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/logs/result.json"
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
  jq -n --arg runner "$runner" --arg hook "$hook" \
    --arg test "printf 'run\\n' >> logs/test-runs; git rev-parse HEAD >> logs/test-heads; python3 -c 'from feature import add; assert add(2, 3) == 5' || exit 1; exit $test_exit" \
    '{agents:{runner:$runner},repo:{test_command:$test,post_implement_command:$hook}}' > "$BUREAU_CONFIG"
  # Mixed reasons without a provider error phrase reach the shell's decision.
  # A mixed result containing "Operation not permitted" retains exit 24;
  # provider_adapter_test.py separately holds that classification.
  jq -n --argjson mixed "$mixed" '{status:"NEEDS_HUMAN",tasks_done:2,tasks_skipped:0,
    tasks_needs_human:(1+$mixed),fixed_review_items:[],notes:{needs_human:
      ([{task_id:"T003",reason:(if $mixed == 1 then "SANDBOX_GATE: socket test cannot bind inside the sandbox" else "SANDBOX_GATE: socket test: Operation not permitted on bind" end)}]
       + if $mixed == 1 then [{task_id:"T002",reason:"A required product decision is missing"}] else [] end),
    skipped:[],deviations:[]},prose_notes:"Other work is finished"}' > "$FAKE_CLAUDE_FIXTURES"
  if [ "$runner" = codex ]; then
    export CODEX_HOME="$SANDBOX/logs/codex-home" FAKE_GATE_MODE=abort
    printf 'def add(a, b):\n    return a - b\n' > "$SANDBOX/feature.py"
    git -C "$SANDBOX" add .gitignore specs feature.py
    git -C "$SANDBOX" commit -qm 'test: genuine edits start from an unfinished feature' -m 'Bureau-Generated: true'
    git -C "$SANDBOX" push -q origin HEAD
    # The same real adapter entry and commit helper as test_codex_implement_pipeline.sh.
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
pathlib.Path('logs/prompt.log').write_text(prompt)
if not os.environ.get('FAKE_CODEX_NO_CHANGE'):
    pathlib.Path('feature.py').write_text('def add(a, b):\n    return a + b\n')
    for path in pathlib.Path('specs').glob('*/tasks.md'):
        path.write_text(path.read_text().replace('[ ]','[X]'))
if os.environ.get('FAKE_GATE_MODE'):
    runpy.run_path('fake-bin/gate_events.py')['emit']()
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(pathlib.Path('logs/result.json').read_text())
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}))
EOF
    chmod +x "$SANDBOX/fake-bin/codex"
    export PATH="$SANDBOX/fake-bin:$PATH"
  fi
}

test_runs() {
  if [ -f "$SANDBOX/logs/test-runs" ]; then wc -l < "$SANDBOX/logs/test-runs" | tr -d ' '; else echo 0; fi
}

assert_handoff_once() {
  assert_eq 0 "$LAST_RC" 'green sandbox gate exit' || return 1
  assert_calls_include 'move_issue.*state-build-review' 'green gate hands off' || return 1
  assert_calls_exclude 'add_issue_label.*needs-human' 'green gate has no human label' || return 1
  assert_eq 1 "$(test_runs)" 'project tests run exactly once' || return 1
}

assert_halted() {
  assert_calls_include 'add_issue_label.*needs-human' 'human label' || return 1
  assert_calls_exclude 'move_issue' 'no hand-off' || return 1
  assert_match 'NEEDS_HUMAN|POST_IMPLEMENT_FAILED' "$LAST_STDOUT" 'halt status' || return 1
}

machine_setup() {
  setup codex 0 "${2:-0}"
  export FAKE_GATE_MODE="${1:-abort}"
  jq --arg cmd "$(jq -r '.repo.test_command' "$BUREAU_CONFIG")" \
    '.notes.needs_human[0] |= (.reason="unprefixed gate aborted" | .kind="gate" | .command=$cmd)' \
    "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/typed.json"
  mv "$SANDBOX/logs/typed.json" "$FAKE_CLAUDE_FIXTURES"
}

machine_teardown() { unset FAKE_GATE_MODE FAKE_CODEX_NO_CHANGE; teardown; }

assert_publication() {
  python3 "$REPO_ROOT/tests/lib/assert_gate_publication.py" "$SANDBOX" implement
}

setup
run_implement_pipeline TEAM-123
assert_handoff_once
assert_publication
assert_file_contains 'SANDBOX_GATE:' "$SANDBOX/logs/prompt.log" 'Codex sandbox prompt'
assert_file_contains 'Optional insights: normally use an empty array' "$SANDBOX/logs/prompt.log" 'implement insight prompt'
assert_calls_include 'shell gate outside the Codex sandbox green' 'decision in summary comment'
assert_file_contains '^def add' "$SANDBOX/feature.py" 'fake Codex left a code change'
assert_match 'Bureau-Generated: true' "$(git -C "$SANDBOX" log --format=%B -1)" 'shell committed the changes'
teardown

setup codex 0 1
run_implement_pipeline TEAM-123
assert_eq 0 "$LAST_RC" 'red sandbox gate keeps the existing labelled NEEDS_HUMAN exit'
assert_halted
assert_eq 1 "$(test_runs)" 'red gate tests ran once'
assert_calls_include 'shell gate outside the Codex sandbox red' 'red decision in summary comment'
assert_publication
teardown

setup codex 1
run_implement_pipeline TEAM-123
assert_eq 0 "$LAST_RC" 'mixed reasons keep the existing NEEDS_HUMAN exit'
assert_halted
assert_eq 0 "$(test_runs)" 'mixed reasons do not run tests'
teardown

setup claude
run_implement_pipeline TEAM-123
assert_eq 0 "$LAST_RC" 'Claude keeps its existing NEEDS_HUMAN exit'
assert_halted
assert_eq 0 "$(test_runs)" 'Claude does not run the Codex gate'
if grep -qF 'Sandbox: this stage runs inside the Codex sandbox' "$SANDBOX/logs/prompt.log"; then
  echo 'FAIL: Claude prompt received the Codex sandbox note' >&2; exit 1
fi
teardown

setup codex 0 0 "printf 'hook\\n' >> logs/hook-runs; echo 'fixture hook failed'; exit 3"
run_implement_pipeline TEAM-123
assert_eq 14 "$LAST_RC" 'failed post-implement hook keeps its existing exit'
assert_halted
assert_eq 1 "$(wc -l < "$SANDBOX/logs/hook-runs" | tr -d ' ')" 'promoted run executes the hook'
assert_eq 0 "$(test_runs)" 'failed hook stops the pending gate'
assert_calls_include 'repo.post_implement_command exited 3' 'hook failure report'
assert_calls_include 'fixture hook failed' 'real hook output in report'
teardown

setup
jq '.repo.test_command = ""' "$BUREAU_CONFIG" > "$SANDBOX/logs/new-config.json"
mv "$SANDBOX/logs/new-config.json" "$BUREAU_CONFIG"
run_implement_pipeline TEAM-123
assert_halted
assert_eq 0 "$(test_runs)" 'missing test command does not promote'
assert_calls_include 'repo.test_command is empty: NEEDS_HUMAN stays' 'missing-command decision in comment'
teardown

# No commits beyond origin/main: everything setup leaves untracked (the spec
# fixture and this test's .gitignore, which the shell's commit step would
# otherwise commit) is committed, origin/main and the branch on origin move to
# that head, and the fake Codex changes nothing. Without the commits floor in
# part 1 the run would be promoted and the test command would run once (and
# fail on the missing feature).
setup
git -C "$SANDBOX" add -A
git -C "$SANDBOX" commit -q --allow-empty -m 'spec fixture'
git -C "$SANDBOX" push -q origin HEAD:main HEAD:test-branch
export FAKE_CODEX_NO_CHANGE=1
run_implement_pipeline TEAM-123
unset FAKE_CODEX_NO_CHANGE
assert_halted
assert_eq 0 "$(test_runs)" 'no commits beyond origin/main do not promote'
assert_calls_include 'no commits beyond origin/main: NEEDS_HUMAN stays' 'no-commit decision in comment'
teardown

# Unprefixed machine execution evidence is sufficient: timeout, kill and red.
for mode in abort kill red; do
  machine_setup "$mode"
  run_implement_pipeline TEAM-123
  assert_handoff_once
  assert_publication
  assert_calls_include 'outside gate logs:.*logs/outside-gates/' 'durable outside log attached'
  python3 - "$SANDBOX" "$mode" <<'PY'
import hashlib, json, pathlib, sys
root=pathlib.Path(sys.argv[1]); receipt=json.loads(next((root/'.git/bureau/evidence/v1').glob('runs/*/receipts/*.json')).read_text())
gate=receipt['gate_attempts'][0]
assert gate['source']=='codex.command_execution' and gate['command_sha256']==gate['gate_id']
assert gate['attempt_id'] and gate['gate_id'] and gate['head']==gate['end_head']
assert gate['normalized_exit']==(124 if sys.argv[2]=='abort' else 137 if sys.argv[2]=='kill' else 1)
assert gate['abort']==('timeout' if sys.argv[2]=='abort' else 'kill' if sys.argv[2]=='kill' else None)
assert len(gate['sha256']) == 64 and gate['bytes'] > 0
assert receipt['structured_result'] is None and len(receipt['result_sha256']) == 64
assert gate['stage_log'] is None
for file in (root/'.git/bureau/evidence/v1').rglob('*'):
    if file.is_file():
        raw=file.read_bytes()
        assert b'machine gate' not in raw and b'from feature import add' not in raw

PY
  machine_teardown
done

machine_setup red 1
run_implement_pipeline TEAM-123
assert_halted
assert_eq 1 "$(test_runs)" 'outside red reruns once'
assert_calls_include 'outside gate logs:.*logs/outside-gates/' 'outside red retains durable log'
assert_publication
machine_teardown

# Removing ONLY the publication link breaks the same genuine-edit handoff.
machine_setup red
python3 - "$SCRIPTS_DIR/bureau-evidence.py" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text()
anchor="        receipt_head = value['end_head']\n"
assert text.count(anchor)==1
p.write_text(text.replace(anchor, anchor+"        if checked_head != receipt_head: return None\n"))
PY
run_implement_pipeline TEAM-123
if assert_handoff_once > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: dropping the publication link passed the genuine-edit assertion' >&2; exit 1
fi
assert_halted
assert_eq 0 "$(test_runs)" 'no publication link prevents implement recovery'
echo 'PASS negative control: dropping the publication link fails genuine-edit implement handoff'
machine_teardown

for mode in stale unknown missing_cwd borrowed_cwd duplicate_begin duplicate_end reordered ambiguous wrong_end agent_bash relative_bash env_agent_bash mixed; do
  machine_setup "$mode"
  if [ "$mode" = mixed ]; then
    jq '.notes.needs_human += [{task_id:"T004",reason:"Product decision remains",kind:"product",command:""}] | .tasks_needs_human=2' \
      "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/mixed.json"
    mv "$SANDBOX/logs/mixed.json" "$FAKE_CLAUDE_FIXTURES"
  fi
  run_implement_pipeline TEAM-123
  assert_halted
  assert_eq 0 "$(test_runs)" "$mode cannot authorize an outside rerun"
  machine_teardown
done

# Negative control: remove ONLY the machine bridge. The unprefixed permission
# result now reproduces the incident's provider stop (24), not a handoff.
machine_setup red
jq '.status="COMPLETE" | .notes.needs_human += [{task_id:"T004",reason:"Product decision remains",kind:"product",command:""}] | .tasks_needs_human=2' \
  "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/contradictory.json"
mv "$SANDBOX/logs/contradictory.json" "$FAKE_CLAUDE_FIXTURES"
run_implement_pipeline TEAM-123
assert_halted
assert_eq 0 "$(test_runs)" 'a claimed COMPLETE cannot erase an explicit product hold'
machine_teardown

machine_setup abort
jq '.notes.needs_human[0].reason="Operation not permitted; gate aborted"' "$FAKE_CLAUDE_FIXTURES" > "$SANDBOX/logs/permission.json"
mv "$SANDBOX/logs/permission.json" "$FAKE_CLAUDE_FIXTURES"
python3 - "$SCRIPTS_DIR/bureau-provider.py" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text(); start=text.index('    machine_gate = (observer')
end=text.index('    if not machine_gate and environment_blocked',start)
p.write_text(text[:start]+'    machine_gate = False\n'+text[end:])
PY
run_implement_pipeline TEAM-123
assert_eq 24 "$LAST_RC" 'negative control: missing bridge reproduces old stop'
if assert_handoff_once > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: no-bridge mutation passed handoff assertion' >&2; exit 1
fi
assert_eq 0 "$(test_runs)" 'no bridge never reaches outside gate'
echo 'PASS negative control: removing machine bridge reproduces exit 24 and fails assert_handoff_once'
machine_teardown

# The prefix is syntax, never failed-execution proof by itself.
setup
unset FAKE_GATE_MODE
run_implement_pipeline TEAM-123
assert_halted
assert_eq 0 "$(test_runs)" 'prefix without proof never spawns outside'
teardown

setup
export FAKE_GATE_MODE=stale
run_implement_pipeline TEAM-123
assert_halted
teardown

# Negative controls run the identical green case through a mutated stage copy.
setup
python3 - "$SCRIPTS_DIR/implement-pipeline.sh" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]); text=path.read_text()
start=text.index('# The shell gate after a Codex turn, part 1.')
end=text.index('# The squash-range check over the finished state',start)
path.write_text(text[:start]+'SANDBOX_GATE_PENDING=0\n\n'+text[end:])
PY
run_implement_pipeline TEAM-123
if assert_handoff_once > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: removing promotion passed the green-case assertion' >&2; exit 1
fi
assert_halted
assert_eq 0 "$(test_runs)" 'without promotion no tests run'
teardown

setup
python3 - "$SCRIPTS_DIR/implement-pipeline.sh" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]); text=path.read_text()
guard=' && [ "$SANDBOX_GATE_PENDING" != 1 ]'
assert text.count(guard)==1
path.write_text(text.replace(guard,''))
PY
run_implement_pipeline TEAM-123
if assert_handoff_once > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: removing the COMPLETE guard passed the green-case assertion' >&2; exit 1
fi
assert_eq 0 "$LAST_RC" 'duplicate-check control still completes'
assert_calls_include 'move_issue.*state-build-review' 'duplicate-check control still hands off'
assert_eq 2 "$(test_runs)" 'without the pending guard tests run twice'
teardown
# An uncertain outside gate stops before routing or any further publication.
for stop_code in 25 124 130; do
  setup
  cat >> "$SCRIPTS_DIR/bureau-config.sh" <<EOF
codex_gate_run() {
  printf '%s\n' '{"eligible":false,"green":false,"reason":"outside gate termination uncertain","failure_cause":"human_hold","logs":["uncertain-gate.log"]}'
  return $stop_code
}
EOF
  run_implement_pipeline TEAM-123
  assert_eq "$stop_code" "$LAST_RC" 'uncertain outside gate preserves its stop'
  assert_calls_exclude 'move_issue' 'uncertain gate never hands off'
  assert_calls_exclude 'add_issue_label.*needs-human' 'uncertain gate stops before routing mutations'
  assert_match 'uncertain-gate.log' "$LAST_STDERR" 'uncertain stop keeps its log pointer'
  teardown
done

setup
cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
codex_gate_run() { printf '%s\n' '{"green":false,"logs":[]}'; return 25; }
EOF
python3 - "$SCRIPTS_DIR/implement-pipeline.sh" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text(); anchor='25|124|130)'
assert text.count(anchor)==1
p.write_text(text.replace(anchor,'124|130)',1))
PY
run_implement_pipeline TEAM-123
if assert_eq 25 "$LAST_RC" 'uncertain gate stop' > "$SANDBOX/logs/control.log" 2>&1; then
  echo 'FAIL: removing the uncertain gate stop passed its exit assertion' >&2; exit 1
fi
echo 'PASS negative control: implement must propagate the uncertain gate hold'
teardown
trap - EXIT
echo 'OK test_codex_sandbox_gate'
