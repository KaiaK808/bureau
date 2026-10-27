#!/bin/bash
# Run the real stages against local Git; only providers/Linear/GitHub are fake.
# Capture full argv per model call so parallel reviewers cannot mix prompts.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
trap 'teardown || true' EXIT
unset BUREAU_CALLER_STOP
STAGE_CASE="${1:-all}"

setup() {
  sandbox_init TEAM-900 001-test-branch
  export BUREAU_CONFIG="$SANDBOX/.bureau.json"
  printf '{"repo":{"test_command":"true"},"agents":{"lessons_budget_bytes":{"spec":600,"implement":600,"qa":600,"code_review":600}}}\n' > "$BUREAU_CONFIG"
  sed -n '/^build_lessons_context() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  sed -n '/^commit_stage_changes() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  git -C "$SANDBOX" checkout -q main
  cat > "$SANDBOX/.gitignore" <<'EOF'
.bureau.json
.env
.fake-origin.git/
scripts/
logs/
prompts/
capture-agent
lessons-before
qa.txt
review.txt
fake_claude_counter
stderr.log
calls.log
EOF
  python3 - "$SANDBOX/LESSONS.md" <<'PY'
import json, pathlib, sys
def entry(text, **meta): return '<!-- bureau-lesson '+json.dumps(meta)+' -->\n'+text+'\n'
pathlib.Path(sys.argv[1]).write_text(entry('GENERAL_LESSON') + entry('TASK_LESSON',paths=['src/selected.py'])
    + entry('DIFF_LESSON',paths=['src/diff-only.py']) + entry('UNRELATED_LESSON',paths=['unrelated/**'])
    + entry('EXPIRED_LESSON',expires='2000-01-01') + entry('TAIL_LESSON '+('tail '*1000)))
PY
  git -C "$SANDBOX" add LESSONS.md .gitignore
  git -C "$SANDBOX" commit -qm 'test: curated lessons'
  git -C "$SANDBOX" push -q origin main
  git -C "$SANDBOX" checkout -q 001-test-branch
  git -C "$SANDBOX" merge -q main
  mkdir -p "$SANDBOX/src"
  printf 'changed only in the branch\n' > "$SANDBOX/src/diff-only.py"
  printf '# Tasks\n- [ ] T001 Edit `src/selected.py`\n' > "$SANDBOX/specs/001-test-branch/tasks.md"
  git -C "$SANDBOX" add src specs
  git -C "$SANDBOX" commit -qm 'test: ticket tasks and branch diff'
  git -C "$SANDBOX" push -q origin 001-test-branch
  mkdir "$SANDBOX/prompts"
  export LESSON_PROMPTS="$SANDBOX/prompts"
  cat > "$SANDBOX/capture-agent" <<'EOF'
#!/bin/bash
python3 - "$@" <<'PY'
import json, os, pathlib, sys, uuid
pathlib.Path(os.environ['LESSON_PROMPTS'], uuid.uuid4().hex+'.json').write_text(json.dumps(sys.argv[1:]))
PY
exec "${LESSON_DELEGATE:?}" "$@"
EOF
  chmod +x "$SANDBOX/capture-agent"
  export FAKE_CLAUDE_BIN="$SANDBOX/capture-agent" LESSON_DELEGATE="$LIB_DIR/fake_claude.sh"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
  export BUREAU_STUB_ISSUE_STATE="$1"
  export BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=0
  unset BUREAU_USE_GOAL_LOOP FAKE_CLAUDE_COMMIT_ON_ITERS
  cp "$SANDBOX/LESSONS.md" "$SANDBOX/lessons-before"
}

check_prompts() {
  python3 - "$SANDBOX/prompts" "$1" "$2" <<'PY'
import json, pathlib, sys
calls=['\n'.join(json.loads(p.read_text())) for p in pathlib.Path(sys.argv[1]).glob('*.json')]
contexts=[]
for prompt in calls:
    if '## Learned patterns' not in prompt: continue
    start=prompt.index('## Learned patterns')
    end=prompt.index('[Lessons truncated to the stage byte budget.]',start)+len('[Lessons truncated to the stage byte budget.]')
    context=prompt[start:end]; contexts.append(context)
    assert len(context.encode('utf-8'))<=600, len(context.encode('utf-8'))
    assert 'GENERAL_LESSON' in context, context
    assert 'Treat as advisory, not binding' in context
    assert 'do not cite them as pinned decisions' in context
    assert 'UNRELATED_LESSON' not in context and 'EXPIRED_LESSON' not in context and 'bureau-lesson' not in context
    if sys.argv[2]!='spec':
        assert 'TASK_LESSON' in context and 'DIFF_LESSON' in context, context
assert len(contexts)==int(sys.argv[3]), (len(calls),len(contexts),sys.argv)
if sys.argv[2]=='review': assert len(set(contexts))==1, 'reviewers received different lessons'
print('PASS '+sys.argv[2]+': '+str(len(contexts))+' bounded prompt(s), paths/general/expiry/wrapper verified')
PY
  [ "$(printf '%s' "$LAST_STDERR" | grep -c 'bureau lessons: WARN: omitted 1 expired lesson(s)' || true)" = 1 ]
  cmp "$SANDBOX/LESSONS.md" "$SANDBOX/lessons-before"
}

check_exit() {
  if [ "$LAST_RC" != "$1" ]; then
    printf '%s\n%s\n' "$LAST_STDOUT" "$LAST_STDERR" >&2
  fi
  assert_eq "$1" "$LAST_RC" "$2"
}

if [ "$STAGE_CASE" = all ] || [ "$STAGE_CASE" = spec ]; then
setup Triage
export LESSON_DELEGATE="$LIB_DIR/pr4-fake-specify.sh" PR4_SPECIFY_ACTION=populate
git -C "$SANDBOX" checkout -q --detach origin/main
run_pipeline spec-pipeline.sh TEAM-900
check_exit 0 'spec completes'
check_prompts spec 1
unset PR4_SPECIFY_ACTION
teardown
fi

if [ "$STAGE_CASE" = all ] || [ "$STAGE_CASE" = implement-loop ]; then
setup Build
export FAKE_CLAUDE_COMMIT_ON_ITERS=1
run_pipeline implement-pipeline.sh TEAM-900
check_exit 0 'iteration implement completes'
check_prompts implement-loop 1
teardown
fi

if [ "$STAGE_CASE" = all ] || [ "$STAGE_CASE" = implement-goal ]; then
setup Build
export BUREAU_USE_GOAL_LOOP=1 FAKE_CLAUDE_COMMIT_ON_ITERS=1
run_pipeline implement-pipeline.sh TEAM-900
check_exit 0 'goal implement completes'
check_prompts implement-goal 1
teardown
fi

if [ "$STAGE_CASE" = all ] || [ "$STAGE_CASE" = qa ]; then
setup QA
export BUREAU_STUB_STATE_QA=state-qa
printf '```json\n{"status":"GREEN","tests_added":0,"tests_failing":0,"coverage_notes":"fixture"}\n```\n' > "$SANDBOX/qa.txt"
export FAKE_CLAUDE_FIXTURES="$SANDBOX/qa.txt"
run_pipeline qa-pipeline.sh TEAM-900
check_exit 0 'QA completes'
check_prompts qa 1
unset BUREAU_STUB_STATE_QA
teardown
fi

if [ "$STAGE_CASE" = all ] || [ "$STAGE_CASE" = review ]; then
setup 'Build Review'
printf 'Review checked.\n```json\n{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"missing_acceptance":[],"fixes_needed":[],"comment":"Review checked.","summary":"fixture"}\n```\n' > "$SANDBOX/review.txt"
export FAKE_CLAUDE_FIXTURES="$SANDBOX/review.txt" GH_STUB_EXISTING_PR=99
run_pipeline code-review-pipeline.sh TEAM-900
check_exit 20 'review honours no-merge boundary'
check_prompts review 3
teardown
fi
echo "OK test_bounded_lessons_stages ($STAGE_CASE)"
