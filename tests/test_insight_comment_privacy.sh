#!/bin/bash
# Run real stages, formatter, parser, comment cap and Linear payload builder.
# Only providers and transport edges are fake; inspect the actual outgoing bodies.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
BASE_PATH="$PATH"
trap 'teardown || true' EXIT
unset BUREAU_CALLER_STOP
export BUREAU_DRY_RUN=0 BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
export BUREAU_IMPL_MAX_ITER=1
export PR7_CANARY='A narrow runner cap required dividing the private canary pass.'
cases=0

setup_privacy() {
  export PATH="$BASE_PATH"
  teardown || true
  sandbox_init TEAM-712 test-branch
  export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
  export BUREAU_CONFIG="$SANDBOX/.bureau.json"
  printf '{"agents":{"merge_mode":"manual"},"repo":{"test_command":"exit %s"}}\n' "$1" > "$BUREAU_CONFIG"
  cat > "$SANDBOX/.gitignore" <<'EOF'
.env
.bureau.json
.fake-origin.git/
.pr2-gh/
scripts/
logs/
*.log
*.jsonl
result.txt
specialist.txt
fake_claude_counter
EOF
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt specs .gitignore
  git -C "$SANDBOX" commit -qm 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  pr2_gate_setup
  # Keep the real post_comment through GraphQL serialization; fake only the
  # UUID lookup and network boundary. Every call appends its complete payload.
  sed -n '/^post_comment() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  sed -n '/^commit_stage_changes() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
_resolve_issue_uuid() { echo fixture-issue-id; }
linear_raw() {
  printf '%s\n' "$1" | jq -c . >> "$SANDBOX/linear-payloads.jsonl"
  echo '{"data":{"commentCreate":{"success":true}}}'
}
EOF
  # Capture GitHub comment/create bodies as argv delivered to gh. Implement
  # gets no existing PR, exercising both the ready and draft create paths.
  mv "$PR2_GH/bin/gh" "$PR2_GH/bin/gh-base"
  cat > "$PR2_GH/bin/gh" <<'EOF'
#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]; root = pathlib.Path(os.environ['SANDBOX'])
if '--body' in args:
    with (root/'github-payloads.jsonl').open('a') as out:
        out.write(json.dumps(dict(argv=args, body=args[args.index('--body')+1])) + '\n')
if args[:2] == ['pr', 'create']:
    print('https://github.com/test-owner/test-repo/pull/99'); sys.exit(0)
if args[:2] == ['pr', 'list'] and os.environ.get('PR7_STAGE') == 'implement':
    print('' if '--jq' in args else '[]'); sys.exit(0)
os.execv(str(pathlib.Path(os.environ['PR2_GH'])/'bin/gh-base'), ['gh-base', *args])
EOF
  chmod +x "$PR2_GH/bin/gh"
}

write_result() {
  python3 - "$REPO_ROOT" "$SANDBOX" "$1" "$2" "$3" <<'PY'
import importlib.util, json, os, pathlib, subprocess, sys, uuid
root, repo = map(pathlib.Path, sys.argv[1:3]); stage, verdict, form = sys.argv[3:]
spec = importlib.util.spec_from_file_location('evidence', root/'templates/scripts/bureau-evidence.py')
e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e)
canary = os.environ['PR7_CANARY']; public = 'PUBLIC_STAGE_SUMMARY'
if stage == 'review':
    value = dict(verdict=verdict, bugs=0, security_issues=0, missing_acceptance=[], fixes_needed=[],
                 summary=public, comment=public, findings=[])
elif stage == 'qa':
    value = dict(status=verdict, tests_added=0, tests_failing=0, coverage_notes=public)
else:
    value = dict(status=verdict, tasks_done=1, tasks_skipped=0, tasks_needs_human=0,
                 fixed_review_items=[], notes=dict(needs_human=[], skipped=[], deviations=[]), prose_notes=public)
value['insights'] = [canary]
assert e.normalize_insights(value)['insights'] == [canary], 'transport canary was not admitted'
# Prove the observation was actually collected locally for this result.
head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
store = e.Store(repo/'.git/bureau'); run = uuid.uuid4().hex
store.begin_run(run, 'TEAM-712', repo, head)
stage_name = {'review':'code_review', 'qa':'qa', 'implement':'implement'}[stage]
attempt = store.begin_attempt(run, 'TEAM-712', stage_name, 'stage', repo, head)
store.put_receipt(dict(run_id=run, issue='TEAM-712', stage=stage_name, phase='stage', provider='claude',
                      provider_attempt_id=uuid.uuid4().hex, stage_attempt_id=attempt, started_at=1, ended_at=2,
                      normalized_exit=0, provider_exit=0, outcome='complete', structured_result=value,
                      start_head=head, end_head=head))
assert canary in (store.root/'insights.jsonl').read_text(), 'local canary observation absent'
if form == 'missing':
    value.pop('comment' if stage == 'review' else 'coverage_notes' if stage == 'qa' else 'prose_notes')
elif form == 'nested':
    key = 'findings' if stage == 'review' else 'coverage_notes' if stage == 'qa' else 'notes'
    value[key] = dict(summary=public, insights=[canary])
text = json.dumps(value)
if form == 'envelope': text = json.dumps(dict(result=text, usage=dict(input_tokens=1)))
elif form == 'fenced': text = public + '\n```json\n' + text + '\n```\n'
elif form == 'malformed': text = public + '\n```json\n' + text[:-1] + '\n```\n'
(repo/'result.txt').write_text(text)
(repo/'specialist.txt').write_text('```json\n{"counts":{"critical":0},"findings":[]}\n```\n')
PY
  if [ "$3" = cap-failure ]; then
    echo 'bureau_cap_comment() { return 1; }' >> "$SCRIPTS_DIR/bureau-config.sh"
  elif [ "$3" = projector-failure ]; then
    cat >> "$SCRIPTS_DIR/bureau-config.sh" <<'EOF'
python3() {
  case "$*" in *'def scrub(value):'*) return 1 ;; esac
  command python3 "$@"
}
EOF
  fi
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/result.txt" FAKE_CLAUDE_MERGE_FIXTURE="$SANDBOX/result.txt"
}

assert_transport_privacy() {
  python3 - "$SANDBOX" "$1" "$2" "$3" <<'PY'
import json, os, pathlib, sys
repo = pathlib.Path(sys.argv[1]); stage, verdict, form = sys.argv[2:]
linear = [json.loads(line) for line in (repo/'linear-payloads.jsonl').read_text().splitlines()]
pr = json.loads((repo/'.pr2-gh/comments.json').read_text())
github_path = repo/'github-payloads.jsonl'
github = [json.loads(line) for line in github_path.read_text().splitlines()] if github_path.exists() else []
bodies = [p['variables']['body'] for p in linear] + [p['body'] for p in github]
assert linear, 'no actual Linear payload captured'
if stage in ('review', 'implement'): assert github, 'no actual GitHub payload captured'
for body in bodies:
    assert os.environ['PR7_CANARY'] not in body, 'insight leaked into actual transport payload: ' + stage + '/' + verdict + '/' + form
    assert '"insights"' not in body, 'insights field leaked into comment projection'
if form not in ('missing', 'malformed', 'projector-failure') and stage != 'implement':
    assert any('PUBLIC_STAGE_SUMMARY' in b for b in bodies), 'public stage summary was lost'
if stage == 'review' and form != 'malformed':
    expected = 'BLOCK' if form in ('missing', 'nested') else verdict
    assert any('**Verdict**: ' + expected in p['body'] for p in pr), 'review routing changed'
if stage == 'qa' and form != 'malformed':
    header = {'GREEN':'QA **PASSED**', 'RED':'tests failing', 'NEEDS_HUMAN':'flagged for human review'}[verdict]
    assert any(header in p['variables']['body'] for p in linear), 'QA routing changed'
if stage == 'implement' and verdict == 'NEEDS_HUMAN' and form != 'malformed':
    assert 'needs-human' in (repo/'calls.log').read_text(), 'implement human hold was lost'
print('PASS transport privacy ' + stage + '/' + verdict + '/' + form + ': ' + str(len(linear)) + ' Linear, ' + str(len(github)) + ' GitHub')
PY
  cases=$((cases + 1))
}

for stage in ${PR7_STAGES:-review qa implement}; do
  export PR7_STAGE="$stage"
  case "$stage" in
    review) verdicts='APPROVE REQUEST_CHANGES BLOCK' ;;
    qa) verdicts='GREEN RED NEEDS_HUMAN' ;;
    implement) verdicts='COMPLETE PARTIAL NEEDS_HUMAN' ;;
  esac
  for verdict in ${PR7_VERDICTS:-$verdicts}; do
    for form in ${PR7_FORMS:-object envelope fenced missing nested malformed cap-failure projector-failure}; do
      test_exit=0
      [ "$stage:$verdict" != qa:RED ] || test_exit=1
      setup_privacy "$test_exit"
      write_result "$stage" "$verdict" "$form"
      case "$stage" in
        review)
          export BUREAU_STUB_ISSUE_STATE='Build Review'
          export FAKE_CLAUDE_FIXTURES="$SANDBOX/specialist.txt"
          run_pipeline code-review-pipeline.sh TEAM-712
          ;;
        qa)
          export BUREAU_STUB_ISSUE_STATE=QA BUREAU_STUB_STATE_QA=state-qa
          run_pipeline qa-pipeline.sh TEAM-712
          ;;
        implement)
          export BUREAU_STUB_ISSUE_STATE=Build FAKE_CLAUDE_COMMIT_ON_ITERS=1
          run_pipeline implement-pipeline.sh TEAM-712
          unset FAKE_CLAUDE_COMMIT_ON_ITERS
          ;;
      esac
      # A malformed result fails closed to BLOCK in review; the other stages
      # keep their pre-existing objective/default decisions.
      expected_rc=0
      if [ "$stage" = review ] && { [ "$verdict" = BLOCK ] || [ "$form" = malformed ] || [ "$form" = missing ] || [ "$form" = nested ]; }; then expected_rc=25; fi
      assert_eq "$expected_rc" "$LAST_RC" "$stage/$verdict/$form exit: $LAST_STDERR"
      assert_transport_privacy "$stage" "$verdict" "$form"
    done
  done
done
echo "PASS $cases stage/verdict/format transport cases; admitted insights retained locally"
