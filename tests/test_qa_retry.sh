#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
BASE_PATH="$PATH"
PYTHONDONTWRITEBYTECODE=1 python3 "$REPO_ROOT/tests/qa_retry_test.py"

setup() {
  export PATH="$BASE_PATH"
  sandbox_init
  sed -n '/^commit_stage_changes() {/,/^# Build the model invocation/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  export BUREAU_STUB_STATE_QA=state-qa BUREAU_STUB_ISSUE_STATE=QA
  export BUREAU_CONFIG="$SANDBOX/.bureau.json"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/result.txt"
  printf '```json\n{"status":"GREEN","tests_added":0,"tests_failing":0,"coverage_notes":"Coverage checked."}\n```\n' > "$FAKE_CLAUDE_FIXTURES"
  cat > "$SANDBOX/.gitignore" <<'EOF'
.bureau.json
.env
.fake-origin.git/
scripts/
logs/
*.log
__pycache__/
result.txt
fake_claude_counter
EOF
  cat > "$SANDBOX/test_gate.py" <<'PY'
from pathlib import Path
import unittest

class Cases(unittest.TestCase):
    def test_stable(self):
        path = Path('logs/stable-count')
        path.write_text(str(int(path.read_text()) + 1 if path.exists() else 1))

    def test_flaky(self):
        path = Path('logs/test-count')
        count = int(path.read_text()) + 1 if path.exists() else 1
        path.write_text(str(count))
        if Path('logs/initial-denial').exists():
            self.assertNotEqual(count, 1, 'Permission denied: fixture environment')
            return
        self.assertFalse(count >= 2 if Path('logs/fail-twice').exists() else count == 2)
PY
  printf '{"repo":{"test_command":"python3 -m unittest -q test_gate"}}' > "$BUREAU_CONFIG"
  git -C "$SANDBOX" add .gitignore test_gate.py specs
  git -C "$SANDBOX" commit -qm 'test: QA gate fixture'
  git -C "$SANDBOX" push -q origin HEAD
  read -r BUREAU_RUN_ID BUREAU_STAGE_ATTEMPT_ID < <(python3 - "$REPO_ROOT" "$SANDBOX" <<'PY'
import importlib.util, os, pathlib, subprocess, sys, uuid
spec = importlib.util.spec_from_file_location('evidence', pathlib.Path(sys.argv[1]) / 'templates/scripts/bureau-evidence.py')
e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e)
repo = pathlib.Path(sys.argv[2])
head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True, timeout=5).strip()
store = e.Store(repo / '.git/bureau'); run = uuid.uuid4().hex
issue = os.environ['BUREAU_STUB_PICKED_ISSUE']
store.begin_run(run, issue, repo, head)
attempt = store.begin_attempt(run, issue, 'qa', 'stage', repo, head)
print(run, attempt)
PY
  )
  export BUREAU_RUN_ID BUREAU_STAGE_ATTEMPT_ID
}

assert_recovered() {
  assert_match 'Status: GREEN' "$LAST_STDOUT" 'recovered QA is green' || return 1
  assert_calls_include 'Flaky: passed on one rerun; failed tests: test_gate.Cases.test_flaky' 'comment reports flake' || return 1
  assert_calls_include 'move_issue.*state-build-review' 'recovered QA hands off' || return 1
}

setup
run_pipeline qa-pipeline.sh "$BUREAU_STUB_PICKED_ISSUE"
if [ "$LAST_RC" != 0 ]; then printf '%s\n%s\n' "$LAST_STDOUT" "$LAST_STDERR" >&2; fi
assert_eq 0 "$LAST_RC" 'QA fixture exit'
assert_recovered
assert_eq 3 "$(cat "$SANDBOX/logs/test-count")" 'one final retry'
assert_eq 2 "$(cat "$SANDBOX/logs/stable-count")" 'rerun selects failed tests'
python3 - "$REPO_ROOT" "$SANDBOX" <<'PY'
import importlib.util, json, os, pathlib, sys
root, repo = map(pathlib.Path, sys.argv[1:])
def module(name):
    spec = importlib.util.spec_from_file_location(name, root / 'templates/scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result); return result
e, a = module('bureau-evidence'), module('bureau-acceptance')
store = e.Store(repo / '.git/bureau')
manifest = store._manifest(os.environ['BUREAU_RUN_ID'])
attempt = manifest['attempts'][0]; gates = attempt['gates']
assert [gate['normalized_exit'] for gate in gates if gate['kind'] == 'gate'] == [0, 1, 0], gates
assert gates[2]['flaky']['failed_tests'] == ['test_gate.Cases.test_flaky'], gates
assert len(list((repo / 'logs').glob('qa-*/final*.log'))) == 2
for path in (repo / 'logs').glob('qa-*/final*.log'): assert path.read_text()
store.set_pr(manifest['run_id'], attempt['attempt_id'], 1)
packet = a.render(store, repo, manifest['issue'], 1, attempt['start_head'], 'https://example.invalid/repo/pull/1', base=attempt['start_head'])
assert 'Flaky: passed on one rerun; failed tests: test_gate.Cases.test_flaky' in packet, packet
for _ in range(9):
    store.gate(manifest['run_id'], attempt['attempt_id'], next((repo / 'logs').glob('qa-*/initial.log')),
               attempt['start_head'], attempt['start_head'], 'configured gate', 0, command_id='qa.test')
packet = a.render(store, repo, manifest['issue'], 1, attempt['start_head'], 'https://example.invalid/repo/pull/1', base=attempt['start_head'])
assert 'Flaky: passed on one rerun; failed tests: test_gate.Cases.test_flaky' in packet, packet
PY
teardown

setup
: > "$SANDBOX/logs/fail-twice"
run_pipeline qa-pipeline.sh "$BUREAU_STUB_PICKED_ISSUE"
assert_eq 0 "$LAST_RC" 'red keeps ordinary QA exit'
assert_match 'Status: RED' "$LAST_STDOUT" 'two failures stay red'
assert_calls_include 'move_issue.*state-build$' 'two failures bounce to Build'
assert_calls_exclude 'Flaky:' 'two failures have no flake marker'
assert_eq 3 "$(cat "$SANDBOX/logs/test-count")" 'no third final attempt'
teardown

setup
: > "$SANDBOX/logs/initial-denial"
run_pipeline qa-pipeline.sh "$BUREAU_STUB_PICKED_ISSUE"
assert_eq 0 "$LAST_RC" 'initial environment failure keeps legacy retry'
assert_match 'Status: GREEN' "$LAST_STDOUT" 'initial retry recovers QA'
assert_calls_include 'qa-pipeline retry-1: initial test run exited 1, retry passed' 'initial retry keeps its comment'
assert_calls_exclude 'Flaky: passed on one rerun' 'initial retry is separate from final flake policy'
assert_eq 3 "$(cat "$SANDBOX/logs/test-count")" 'initial run, retry and final run'
assert_eq 3 "$(cat "$SANDBOX/logs/stable-count")" 'initial retry repeats the whole gate'
python3 - "$REPO_ROOT" "$SANDBOX" <<'PY'
import importlib.util, os, pathlib, sys
root, repo = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('evidence', root / 'templates/scripts/bureau-evidence.py')
e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e)
gates = e.Store(repo / '.git/bureau')._manifest(os.environ['BUREAU_RUN_ID'])['attempts'][0]['gates']
gates = [gate for gate in gates if gate['kind'] == 'gate']
assert [gate['normalized_exit'] for gate in gates] == [1, 0, 0], gates
assert all('flaky' not in gate for gate in gates), gates
logs = list((repo / 'logs').glob('qa-*/initial*.log'))
assert len(logs) == 2 and all(log.read_text() for log in logs), logs
assert any('Permission denied: fixture environment' in log.read_text() for log in logs)
PY
teardown

# The same recovery assertions reject a disabled final-gate rerun.
setup
python3 - "$SCRIPTS_DIR/bureau-gates.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1]); source = path.read_text()
needle = '    first = qa_test_output(log)\n'
assert source.count(needle) == 1, 'final-gate rerun mutation must apply exactly once'
path.write_text(source.replace(needle, '    return code, abort, result\n' + needle))
PY
run_pipeline qa-pipeline.sh "$BUREAU_STUB_PICKED_ISSUE"
assert_match 'Status: RED' "$LAST_STDOUT" 'mutation has no final rerun'
if assert_recovered >/dev/null 2>&1; then echo 'FAIL: disabled rerun escaped the recovery control' >&2; exit 1; fi
assert_eq 2 "$(cat "$SANDBOX/logs/test-count")" 'mutation runs the final gate only once'
echo 'OK test_qa_retry'
