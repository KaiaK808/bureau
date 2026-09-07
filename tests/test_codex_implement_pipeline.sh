#!/bin/bash
# Real provider adapter + implementation loop; only external APIs/model are fake.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
sandbox_init EXP-777 test-branch
trap teardown EXIT
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
cat > "$SANDBOX/.bureau.json" <<'EOF'
{"agents":{"runner":"codex","use_goal_loop":true},"repo":{"test_command":"python3 -c 'from feature import add; assert add(2, 3) == 5'"}}
EOF
export BUREAU_CONFIG="$SANDBOX/.bureau.json"
export BUREAU_STUB_RUNNER=codex BUREAU_USE_GOAL_LOOP=1
# Use the real adapter entry and shell commit function with stub Linear calls.
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
assert '--append-system-prompt' not in sys.argv
prompt=sys.stdin.read()
assert '/goal ' not in prompt
pathlib.Path('feature.py').write_text('def add(a, b):\n    return a + b\n')
for path in pathlib.Path('specs').glob('*/tasks.md'): path.write_text(path.read_text().replace('[ ]','[X]'))
value={'status':'COMPLETE','tasks_done':3,'tasks_skipped':0,'tasks_needs_human':0,'fixed_review_items':[],
       'notes':{'needs_human':[],'skipped':[],'deviations':[]},'prose_notes':'Implemented and checked'}
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(value))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}))
EOF
chmod +x "$SANDBOX/fake-bin/codex"
export PATH="$SANDBOX/fake-bin:$PATH"
run_implement_pipeline EXP-777
if [ "$LAST_RC" != 0 ]; then printf '%s\n%s\n' "$LAST_STDOUT" "$LAST_STDERR"; fi
assert_eq 0 "$LAST_RC" 'Codex implementation completes'
assert_calls_include 'move_issue.*state-build-review'
assert_match 'Bureau-Generated: true' "$(git -C "$SANDBOX" log --format=%B -3)" 'shell commits have provider-neutral provenance'
assert_calls_exclude 'precondition_claude_auth'
jq '.repo.test_command = "false"' "$BUREAU_CONFIG" > "$SANDBOX/new-config.json"
mv "$SANDBOX/new-config.json" "$BUREAU_CONFIG"
run_implement_pipeline EXP-777
assert_eq 14 "$LAST_RC" 'independent failing test blocks completion'
assert_calls_exclude 'move_issue'
echo 'OK test_codex_implement_pipeline'
