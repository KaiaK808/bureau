#!/bin/bash
# Real adapter, pipelines and Git origin; only provider/API calls are fake.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
sandbox_init EXP-778 test-branch
trap teardown EXIT
cat > "$SANDBOX/.gitignore" <<'EOF'
.fake-origin.git/
scripts/
logs/
*.log
fake-bin/
__pycache__/
EOF
# Deliberately leave local .env and .bureau.json unignored to verify that the
# executor's publication helper excludes them even when it collects new files.
cat > "$SANDBOX/.bureau.json" <<'EOF'
{"agents":{"runner":"codex"},"repo":{"test_command":"python3 -m unittest discover -s tests -p 'test*.py'"}}
EOF
mkdir -p "$SANDBOX/tests" "$SANDBOX/fake-bin"
echo '# Test directory' > "$SANDBOX/tests/.keep"
git -C "$SANDBOX" add .gitignore specs tests
git -C "$SANDBOX" commit -qm 'test: baseline with spec and test directory'
git -C "$SANDBOX" push -q origin HEAD
export BUREAU_CONFIG="$SANDBOX/.bureau.json" BUREAU_STUB_RUNNER=codex
export BUREAU_STUB_STATE_QA=state-qa BUREAU_STUB_STATE_COPY=state-copy
# Production invocation/auth/commit helpers, with the harness's Linear stubs.
sed -n '/^run_stage_for() {/,/^# Build the model invocation/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
BUREAU_RUNTIME="$(dirname "$0")/bureau-runtime.py"
BUREAU_LABEL_NEEDS_COPY_NAME=needs-copy
EOF
cat > "$SANDBOX/fake-bin/codex" <<'EOF'
#!/usr/bin/env python3
import json, os, pathlib, sys
if sys.argv[1]=='login': sys.exit(0)
assert sys.argv[1]=='exec'
assert 'Do not run git commit or git push.' in sys.stdin.read()
stage=os.environ['FAKE_STAGE']
if stage=='qa':
    name=os.environ.get('FAKE_TEST_FILE','tests/test_added.py')
    pathlib.Path(name).write_text('import unittest\nclass Added(unittest.TestCase):\n    def test_add(self): self.assertEqual(2 + 3, 5)\n')
    value={'status':'GREEN','tests_added':1,'tests_failing':0,'coverage_notes':'Added a passing regression test'}
elif stage=='spec_review':
    pathlib.Path('specs/001-branch/acceptance.md').write_text('Acceptance detail\n')
    value={'review_status':'PASS','ui_work_needed':False,'issues_found':1,'issues_fixed':1,'remaining_issues':[],'summary':'Added acceptance detail'}
else:
    pathlib.Path('messages.json').write_text('{"submit":"Send"}\n')
    value={'strings_changed':1,'changes':[],'open_questions':[],'summary':'Added copy resource'}
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(value))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}))
EOF
chmod +x "$SANDBOX/fake-bin/codex"
export PATH="$SANDBOX/fake-bin:$PATH"
for stage in qa spec_review copy; do
  case "$stage" in
    qa) state=QA; file=tests/test_added.py; next='state-build-review' ;;
    spec_review) state='Spec Review'; file=specs/001-branch/acceptance.md; next='state-build' ;;
    copy) state=Copy; file=messages.json; next='state-build' ;;
  esac
  export FAKE_STAGE="$stage" BUREAU_STUB_ISSUE_STATE="$state"
  run_pipeline "$(printf '%s' "$stage" | tr '_' '-')-pipeline.sh" EXP-778
  if [ "$LAST_RC" != 0 ]; then printf '%s\n%s\n' "$LAST_STDOUT" "$LAST_STDERR"; fi
  assert_eq 0 "$LAST_RC" "$stage publishes new files"
  assert_calls_include "move_issue.*$next"
  assert_eq "$file" "$(git -C "$SANDBOX" ls-tree --name-only origin/test-branch -- "$file")" "$stage file reached origin"
  assert_eq '' "$(git -C "$SANDBOX" ls-tree --name-only origin/test-branch -- .env .bureau.json logs)" 'private runtime files stay local'
done
assert_match 'Bureau-Generated: true' "$(git -C "$SANDBOX" log -1 --format=%B)" 'executor commit provenance'
# Preserve the whole index when private files are already staged: merely
# excluding them from the helper's git-add list must not allow a credential
# commit. Keep ordinary staged work intact alongside the private addition.
printf '{"submit":"Send now"}\n' > "$SANDBOX/messages.json"
git -C "$SANDBOX" add .env messages.json
STAGED_BEFORE=$(git -C "$SANDBOX" diff --cached --binary)
HEAD_BEFORE=$(git -C "$SANDBOX" rev-parse HEAD)
export FAKE_STAGE=qa BUREAU_STUB_ISSUE_STATE=QA FAKE_TEST_FILE=tests/test_private_stage.py
run_pipeline qa-pipeline.sh EXP-778
assert_eq 24 "$LAST_RC" 'staged credentials block publication'
assert_calls_exclude 'move_issue'
assert_eq "$HEAD_BEFORE" "$(git -C "$SANDBOX" rev-parse HEAD)" 'blocked publication creates no commit'
assert_eq "$STAGED_BEFORE" "$(git -C "$SANDBOX" diff --cached --binary)" 'staged private and intended changes are preserved'
assert_file_contains 'LINEAR_API_KEY=test-key' "$SANDBOX/.env" 'credential file retained locally'
# Explicitly unstage only the private file, then confirm the ordinary staged
# change and new test are published by the real pipeline on retry.
git -C "$SANDBOX" reset -q HEAD -- .env
run_pipeline qa-pipeline.sh EXP-778
assert_eq 0 "$LAST_RC" 'publication succeeds after private file is unstaged'
assert_calls_include 'move_issue.*state-build-review'
assert_eq '{"submit":"Send now"}' "$(git -C "$SANDBOX" show origin/test-branch:messages.json)" 'ordinary staged changes are published'
assert_eq tests/test_private_stage.py "$(git -C "$SANDBOX" ls-tree --name-only origin/test-branch -- tests/test_private_stage.py)" 'new test is published on retry'
assert_eq '' "$(git -C "$SANDBOX" ls-tree --name-only origin/test-branch -- .env)" 'credential file was never published'
# Rejected publication must not report successful QA or advance the issue.
printf '#!/bin/sh\nexit 1\n' > "$SANDBOX/.fake-origin.git/hooks/pre-receive"
chmod +x "$SANDBOX/.fake-origin.git/hooks/pre-receive"
export FAKE_STAGE=qa BUREAU_STUB_ISSUE_STATE=QA FAKE_TEST_FILE=tests/test_rejected.py
run_pipeline qa-pipeline.sh EXP-778
assert_eq 18 "$LAST_RC" 'rejected push blocks stage completion'
assert_calls_exclude 'move_issue'
assert_eq tests/test_rejected.py "$(git -C "$SANDBOX" ls-tree --name-only HEAD -- tests/test_rejected.py)" 'unpublished progress retained locally'
assert_eq '' "$(git -C "$SANDBOX" ls-tree --name-only origin/test-branch -- tests/test_rejected.py)" 'rejected file absent from origin'
echo 'OK test_codex_stage_publication'
