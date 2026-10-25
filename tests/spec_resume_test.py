"""Real Git and real worker/reset path; only service/provider calls are faked."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import git_maintenance
from governance_cases import (FENCED_GOVERNANCE, GOVERNANCE_FIRST,
                              COMMENT_HIDDEN_GOVERNANCE, COLUMN_ZERO_GOVERNANCE,
                              ROUND8_INVALID, ROUND8_VALID, ROUND9_INVALID, ROUND9_VALID)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_TEST_SPEC_SCRIPTS', ROOT / 'templates/scripts'))
module = importlib.util.spec_from_file_location('runtime_spec_tests', SCRIPTS / 'bureau-runtime.py')
r = importlib.util.module_from_spec(module); module.loader.exec_module(r)
VALID = '# Feature\n\n## Governance and security\nDecision: not required\nRationale: This documentation change handles no credentials or personal data.\n'


class GovernanceTests(unittest.TestCase):
    def test_wrapped_placeholders_and_indented_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'spec.md'
            for name, body in ROUND9_INVALID.items():
                with self.subTest(case=name):
                    path.write_text(body)
                    self.assertIsNotNone(r.s.governance_error(path))
            for name, body in ROUND9_VALID.items():
                with self.subTest(case=name, valid=True):
                    path.write_text(body)
                    self.assertIsNone(r.s.governance_error(path))

    def test_round8_honest_mistakes_and_valid_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'spec.md'
            for name, body in ROUND8_INVALID.items():
                with self.subTest(case=name):
                    path.write_bytes(body.encode('utf-8'))
                    self.assertIsNotNone(r.s.governance_error(path))
            for name, body in ROUND8_VALID.items():
                with self.subTest(case=name, valid=True):
                    path.write_bytes(body.encode('utf-8'))
                    self.assertIsNone(r.s.governance_error(path))

    def test_governance_prompt_contract(self):
        contract = ('In spec.md, place ## Governance and security with Decision: and Rationale: lines at column 0, '
                    'in plain text, before any code block or HTML block; a leading YAML front matter block is allowed.')
        for relative in ('templates/scripts/spec-pipeline.sh', 'templates/scripts/spec-review-pipeline.sh', 'templates/scripts/bureau-stage.md',
                         'templates/commands/linear-to-spec.md', 'templates/instructions/workflow.md'):
            with self.subTest(path=relative):
                self.assertIn(contract, (ROOT/relative).read_text().replace('`', ''))

    def test_governance_precedes_first_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'spec.md'
            for name, body in FENCED_GOVERNANCE.items():
                with self.subTest(case=name):
                    path.write_text(body)
                    self.assertIn('move the Governance and security section above the first code block',
                                  r.s.governance_error(path) or '')
            for name, body in GOVERNANCE_FIRST.items():
                with self.subTest(case=name, valid=True):
                    path.write_text(body); self.assertIsNone(r.s.governance_error(path))

    def test_governance_comments_and_column_zero_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'spec.md'
            for name, body in {**COMMENT_HIDDEN_GOVERNANCE, **COLUMN_ZERO_GOVERNANCE}.items():
                with self.subTest(case=name):
                    path.write_text(body); self.assertIsNotNone(r.s.governance_error(path))

    def test_invalid_sections_and_justified_decisions(self):
        cases = ('# Feature\n', '## Governance and security\n',
                 '## Governance and security\nTBD\n',
                 '## Governance and security\nDecision: N/A\nRationale: N/A\n',
                 '## Governance and security\nDecision: required\nRationale: TODO later\n',
                 '## Governance and security\nDecision: not required\nRationale: <explain rationale here>\n',
                 '## Governance and security\nRationale: N/A\nDecision: not required for this change\n',
                 '## Governance and security\nDecision: not required\nRationale:\n### Missing explanation\n',
                 '```md\n' + VALID + '```\n', '<!--\n' + VALID + '-->\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'spec.md'
            for value in cases:
                path.write_text(value)
                with self.subTest(value=value): self.assertIsNotNone(r.s.governance_error(path))
            for value in (VALID, VALID.replace('not required', 'required').replace('This documentation change handles no credentials or personal data.', 'Restrict access to credentials and redact personal data from audit logs.'),
                          '## Governance and security\nNot required because this changes only static documentation.\n'):
                path.write_text(value); self.assertIsNone(r.s.governance_error(path))
    def test_reviewer_placeholder_and_german_decision_matrix(self):
        invalid=(
            'Decision: not required\nRationale: T.B.D. after security review.',
            'Decision: not required\nRationale: To be defined after review.',
            'Decision: required\nRationale: tbd after review.',
            'Decision: required\nRationale: t.b.d. after review.',
            'Decision: required\nRationale: T-B-D after checks are complete.',
            'Decision: required\nRationale: T_B_D after checks are complete.',
            'Decision: required\nRationale: T**B**D after checks are complete.',
            'Decision: required\nRationale: To-be-defined after checks are complete.',
            'Decision: required\nRationale: T . B . D after review.',
            'Entscheidung: erforderlich\nBegründung: Wird noch geklärt.',
            'Decision: N/A\n- Access:\n- Logging:\n- Retention:',
            '**Decision:** N/A\n* Access:\n* Logging:\n* Retention:',
            'Decision: N/A\nRationale:\n- [ ] Access:\n- [ ] Logging:\n- [ ] Retention:',
            'Entscheidung: nicht erforderlich\nBegründung: N/A',
            'Decision: not required\nRationale: Access: Logging: Retention:',
            'Decision: N/A\nRationale: Not required.',
            'Entscheidung: nicht erforderlich\nBegründung: Nicht erforderlich.',
            'Decision: required\nRationale: Explanation pending security review.',
            'Entscheidung: erforderlich\nBegründung: Die Begründung folgt nach dem Review.',
            'Decision: required\nRationale: TBD',
            'Decision: required\nRationale: tbc',
            'Decision: required\nRationale: pending',
            'Decision: required\nRationale: Pending security review',
            'Decision: required\nRationale: to follow',
            'Decision: required\nRationale: To be decided',
            'Entscheidung: erforderlich\nBegründung: folgt',
            'Entscheidung: erforderlich\nBegründung: wird nachgereicht',
            'Entscheidung: erforderlich\nBegründung: offen',
            'Decision: required\nRationale: -',
            'Decision: required\nRationale:\n- TBD',
            'Decision: required\nRationale:\n1. TODO',
            'Decision: required\nRationale: [TBD]',
            'Decision: N/A\n- Access: To follow.\n- Logging: To follow.\n- Retention: To follow.',
        )
        valid=(
            'Nicht erforderlich, weil diese Änderung ausschließlich statische Dokumentation betrifft',
            'Decision: Nicht erforderlich, weil diese Änderung ausschließlich statische Dokumentation betrifft',
            'Entscheidung: Nicht erforderlich, weil diese Änderung ausschließlich statische Dokumentation betrifft',
            'Entscheidung: erforderlich\nBegründung: Die Änderung verarbeitet personenbezogene Daten.',
            'Decision: not required\nBegründung: Diese Änderung berührt keine Zugangsdaten.',
            'Entscheidung: nicht erforderlich\nRationale: This change processes no credentials.',
            'Decision: N/A\nRationale: This change only affects static documentation.',
            'Nicht anwendbar, da die Änderung ausschließlich statische Dokumentation betrifft.',
            'Decision: required\nRationale:\n- Access: Restrict access to credentials.\n- Logging: Redact personal data.',
            'Decision: not applicable\nRationale: It is static.',
            'Decision: required\nRationale: Authentication changed.',
            'Entscheidung: nicht erforderlich\nBegründung: Alle Inhalte bleiben statisch.',
            'Entscheidung: nicht erforderlich\nBegründung: Alle Inhalte bleiben statisch',
            'Decision: not required\nRationale: All permissions remain unchanged',
            'Entscheidung: erforderlich\nBegründung: Die Aufbewahrungsdauer wird definiert durch die bestehende Löschrichtlinie.',
            'Decision: required\nRationale: The pending queue contains no personal data',
            'Decision: required\nRationale: Pending events are logged without payloads',
            'Entscheidung: erforderlich\nBegründung: Der Zugriff folgt der bestehenden Rollenrichtlinie',
            # Deliberately vague/circular but structurally valid: model review
            # owns adequacy, and its FAIL path is exercised below.
            'Decision: required\nRationale: Required because governance is required',
            'Decision: required\nRationale: Appropriate controls',
        )
        for name in ('Access', 'Logging', 'Retention'):
            for placeholder in ('', '-', 'N/A', 'Not required.', 'Nicht erforderlich.', 'To follow.', 'Explanation pending security review.'):
                invalid += ('Decision: required\nRationale: Credentials require access controls.\n- '+name+': '+placeholder,)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'spec.md'
            for body in invalid:
                path.write_text('## Governance and security\n'+body+'\n')
                with self.subTest(body=body): self.assertIsNotNone(r.s.governance_error(path))
            for body in valid:
                path.write_text('## Governance and security\n'+body+'\n')
                with self.subTest(body=body): self.assertIsNone(r.s.governance_error(path))


class CheckpointTests(unittest.TestCase):


    def test_standalone_evidence_without_ticket_does_not_create_spec_checkpoint(self):
        with tempfile.TemporaryDirectory() as scratch:
            store=r.e.Store(Path(scratch)/'bureau'); run='a'*32
            store.begin_run(run,None,'worker',None)
            attempt=store.begin_attempt(run,None,'spec','stage','worker',None)
            store.end_attempt(run,attempt,0,None,outer=True)
            self.assertFalse((store.root/'spec-checkpoints').exists())

    def test_files_and_foreign_directory_are_not_completion_evidence(self):
        with tempfile.TemporaryDirectory(prefix='bureau checkpoint ') as scratch:
            repo = Path(scratch).resolve()
            subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
            for args in (git_maintenance.OFF_CONFIG, ('config','user.name','Test'), ('config','user.email','test@invalid'),
                         ('commit','--allow-empty','-qm','base')): r.git(repo,*args)
            checkpoints=r.s.Checkpoints(repo)
            own=repo/'specs/001-team-1'; foreign=repo/'specs/002-team-2'
            for directory in (own,foreign):
                directory.mkdir(parents=True)
                for name in ('spec.md','plan.md','tasks.md'): (directory/name).write_text('stale pre-existing content')
            self.assertEqual(checkpoints.begin('TEAM-1','specs',own,1,{'ticket':'new input'}),[])
            self.assertEqual(checkpoints.path('TEAM-1').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError): checkpoints.begin('TEAM-1','specs',foreign,2,{'ticket':'new input'})


    def test_checkpoint_replacements_reserve_global_capacity(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as scratch:
            repo=Path(scratch)
            subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
            for args in (git_maintenance.OFF_CONFIG, ('config','user.name','Test'), ('config','user.email','test@invalid'),
                         ('commit','--allow-empty','-qm','base')): r.git(repo,*args)
            checkpoints=r.s.Checkpoints(repo)
            own=repo/'specs/001-team-1'; own.mkdir(parents=True)
            (own/'spec.md').write_text(VALID)
            checkpoints.begin('TEAM-1','specs',own,1,{'ticket':'first input'})
            path=checkpoints.path('TEAM-1'); before=path.read_bytes()
            files=[p for p in checkpoints.store.root.rglob('*') if p.is_file() and p.name!='guard']
            for limits in ({'MAX_BYTES':sum(p.stat().st_size for p in files)}, {'MAX_RECORDS':len(files)}):
                with self.subTest(limits=limits), patch.multiple(r.s.e,**limits):
                    with self.assertRaises(r.s.e.CapacityError):
                        checkpoints.begin('TEAM-1','specs',own,1,{'ticket':'second input'})
                self.assertEqual(path.read_bytes(),before)
                self.assertFalse(list(checkpoints.store.root.rglob('*.tmp')))
            checkpoints.invalidate('TEAM-1','specify','PRIVATE_INVALIDATION_SENTINEL')
            self.assertNotIn(b'PRIVATE_INVALIDATION_SENTINEL',path.read_bytes())
            self.assertEqual(json.loads(path.read_text())['invalidation']['reason_sha256'],
                             r.s.e.text_digest('PRIVATE_INVALIDATION_SENTINEL'))

    def test_checkpoint_io_with_closed_stderr_keeps_optional_status(self):
        with tempfile.TemporaryDirectory() as scratch:
            repo=Path(scratch)
            subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
            own=repo/'specs/001-team-1'; own.mkdir(parents=True)
            source='''import importlib.util, os, sys
module=importlib.util.spec_from_file_location('spec_cli',sys.argv[1])
spec=importlib.util.module_from_spec(module); module.loader.exec_module(spec)
del sys.argv[1]
def unavailable(*args,**kwargs): raise OSError('PRIVATE_CHECKPOINT_FAILURE')
spec.Checkpoints.read=unavailable
os.close(2)
sys.exit(spec.main())
'''
            for command in (['begin','TEAM-1','--specs-dir','specs','--directory',str(own),'--number','1'],
                            ['complete','TEAM-1','specify','--commit','a'*40],
                            ['invalidate','TEAM-1','specify','--reason','spec-review-failed'],
                            ['reset-target','TEAM-1','--specs-dir','specs']):
                with self.subTest(command=command[0]):
                    result=subprocess.run([sys.executable,'-c',source,str(SCRIPTS/'bureau-spec.py'),
                                           '--repo',str(repo),*command],input='{}',capture_output=True,text=True,timeout=10)
                    self.assertEqual(result.returncode,2,result.stdout+result.stderr)
                    self.assertNotIn('PRIVATE_CHECKPOINT_FAILURE',result.stdout+result.stderr)
                    self.assertTrue((repo/'.git/bureau/spec-checkpoints-disabled/TEAM-1.json').is_file())

    def test_optional_spec_evidence_startup_disables_reuse_and_keeps_exit(self):
        import argparse
        from unittest.mock import patch
        for method in ('begin_run','begin_attempt'):
            for error in (OSError,ValueError):
                for code in (0,14,25,124):
                    with self.subTest(method=method,error=error,code=code), tempfile.TemporaryDirectory() as scratch:
                        repo=Path(scratch)
                        subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
                        (repo/'.bureau.json').write_text('{}')
                        args=argparse.Namespace(workspace=None,entry='spec-pipeline.sh',issue='TEAM-1',
                            command=['bash','-c','[ "$BUREAU_SPEC_CHECKPOINTS_DISABLED" = 1 ] && '
                                     '[ "${#BUREAU_STAGE_ATTEMPT_ID}" = 32 ] || exit 99; exit '+str(code)])
                        with patch.object(r.e.Store,method,side_effect=error('fixture unavailable')):
                            self.assertEqual(r.execute(repo,args,r.Store(repo)),code)
                        disabled=repo/'.git/bureau/spec-checkpoints-disabled/TEAM-1.json'
                        self.assertEqual(json.loads(disabled.read_text()),dict(issue='TEAM-1',reuse_disabled=True))


class SpecWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau spec resume ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.wt = self.root / 'worker'; self.review_wt = self.root / 'review-worker'
        self.git('init', '-q', '-b', 'main')
        self.git(*git_maintenance.OFF_CONFIG)
        self.git('config', 'user.name', 'Test'); self.git('config', 'user.email', 'test@invalid')
        shutil.copytree(SCRIPTS, self.repo / 'scripts')
        import zipfile
        with zipfile.ZipFile(ROOT / 'tests/fixtures/speckit-1.2.2.zip') as archive:
            for name in ('scripts/bash/common.sh', 'scripts/bash/create-new-feature.sh'):
                archive.extract(name, self.repo / '.specify')
        (self.repo / '.gitignore').write_text('.bureau.json\n.env\n.specify/feature.json\nlogs/\n')
        # Read-only service inputs and call logs live outside the worker tree.
        self.write('state', 'Triage'); self.write('spec-body', VALID)
        self.write('comments.json', '[]')
        self.write('detail.json', json.dumps(dict(title='Feature', description='Ticket input', project=dict(name='Project', description='Context'), labels=dict(nodes=[]))))
        self.write('review.json', json.dumps(dict(review_status='PASS', ui_work_needed=False, issues_found=0, issues_fixed=0, remaining_issues=[], summary='Model PASS')))
        with (self.repo / 'scripts/bureau-config.sh').open('a') as out:
            out.write(r'''
# Test-only fake service boundaries; ownership/reset/checkpoints remain real.
precondition_linear() { :; }
precondition_runner() { :; }
alert_telegram() { :; }
add_issue_label() { printf '%s\t%s\n' "$1" "$2" >> "$SPEC_TEST_ROOT/labels"; }
crosscheck_open_prs() { :; }
get_issue_state() { cat "$SPEC_TEST_ROOT/state"; }
get_issue_detail() { cat "$SPEC_TEST_ROOT/detail.json"; }
get_issue_comments() { cat "$SPEC_TEST_ROOT/comments.json"; }
get_issue_branch() { echo 001-team-1; }
bureau_issue_snapshot() { jq -n --arg state "$(get_issue_state)" '{identifier:"TEAM-1",id:"uuid",state:{id:$state,name:$state},labels:{nodes:[]}}'; }
move_issue() { printf '%s' "$2" > "$SPEC_TEST_ROOT/state"; }
post_comment() { printf '%s' "$2" | python3 -c 'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); v=json.loads(p.read_text()); v.insert(0,{"body":sys.stdin.read()}); p.write_text(json.dumps(v))' "$SPEC_TEST_ROOT/comments.json"; }
run_stage_for() {
  local stage="$1" phase="${BUREAU_PROVIDER_PHASE:-review}" dir
  printf '%s\n' "$phase" >> "$SPEC_TEST_ROOT/calls"
  printf '%s\n' "$2" >> "$SPEC_TEST_ROOT/prompts"
  if [ "$stage" = spec_review ]; then cat "$SPEC_TEST_ROOT/review.json"; return; fi
  dir=$(jq -r .feature_directory .specify/feature.json)
  case "$phase" in
    specify) cp "$SPEC_TEST_ROOT/spec-body" "$dir/spec.md" ;;
    plan)
      if [ -f "$dir/plan.md" ]; then cat "$dir/plan.md" >> "$SPEC_TEST_ROOT/retained-plan"; fi
      printf '# Plan\n' > "$dir/plan.md" ;;
    tasks) printf '# Tasks\n- [ ] T001 ticket task\n' > "$dir/tasks.md" ;;
    digest) echo '**Spec Artifacts — fixture**' ;;
  esac
  if [ "$phase" = "${SPEC_FAIL_PHASE:-}" ]; then
    printf '\npartial phase edits\n' >> "$dir/$phase.md"
    return 124
  fi
}
''')
        config = dict(linear=dict(teams=[dict(key='TEAM', id='team', states=dict(triage='Triage', spec='Spec', spec_review='Spec Review', build='Build', done='Done'))], labels=dict(lane2=dict(name='lane-2'), needs_human=dict(name='needs-human'))), agents=dict(runner='claude'), repo=dict(specs_dir='specs', base_branch='main'))
        self.write_config(config)
        (self.repo / '.env').write_text('LINEAR_API_KEY=fake-test-key\n')
        self.git('add', '.'); self.git('commit', '-qm', 'fixture runtime and pinned helpers')
        origin = self.root / 'origin.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
        subprocess.run(['git', '--git-dir', str(origin), *git_maintenance.OFF_CONFIG], check=True)
        self.git('remote', 'add', 'origin', str(origin)); self.git('push', '-q', 'origin', 'main')
        self.env = {k:v for k,v in os.environ.items() if not k.startswith('BUREAU_')}
        self.env.update(BUREAU_CONFIG=str(self.repo / '.bureau.json'), SPEC_TEST_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1')

    def write(self, name, text): (self.root / name).write_text(text)
    def write_config(self, config): (self.repo / '.bureau.json').write_text(json.dumps(config))
    def git(self, *args, repo=None): return r.git(repo or self.repo, *args)
    def calls(self): return (self.root / 'calls').read_text().splitlines()
    def checkpoint(self): return json.loads((self.repo / '.git/bureau/evidence/v1/spec-checkpoints/TEAM-1.json').read_text())
    def worker(self, fail='', review=False, expect=0):
        wt = self.review_wt if review else self.wt
        pipeline = 'spec-review-pipeline.sh' if review else 'spec-pipeline.sh'
        args = ['bash', str(self.repo / 'scripts/bureau-worker.sh'), 'TEAM-1', pipeline, str(wt)]
        if review: args.append('001-team-1')
        proc = subprocess.run(args, cwd=self.repo, env={**self.env, 'SPEC_FAIL_PHASE':fail}, capture_output=True, text=True, timeout=40)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def test_plan_timeout_resumes_plan_and_tasks_through_real_reset(self):
        self.worker('plan', expect=124)
        saved = self.checkpoint()
        self.assertEqual(saved['schema_version'], 1)
        self.assertEqual(saved['checkpoint_version'], 2)
        self.assertEqual(list(saved['phases']), ['specify'])
        self.assertEqual(saved['spec_directory'], 'specs/001-team-1')
        commit = saved['phases']['specify']['commit']
        self.assertEqual(self.git('rev-parse', '001-team-1'), commit)
        self.assertIn('TEAM-1: spec specify checkpoint', self.git('show', '-s', '--format=%B', commit))
        self.assertEqual(saved['phases']['specify']['artifacts']['spec.md'], hashlib.sha256(VALID.encode()).hexdigest())
        self.assertFalse(self.git('for-each-ref', '--format=%(refname)', 'refs/remotes/origin/001-team-1'))
        (self.wt / '.specify/feature.json').unlink()
        proc = self.worker()
        self.assertIn('Resetting spec worker to committed checkpoint ' + commit, proc.stdout)
        self.assertEqual(self.calls(), ['specify', 'plan', 'plan', 'tasks', 'digest'])
        self.assertFalse((self.root / 'retained-plan').exists())
        phases = self.checkpoint()['phases']
        self.assertEqual(list(phases), ['specify', 'plan', 'tasks'])
        commits = [phases[phase]['commit'] for phase in r.s.PHASES]
        self.assertEqual(len(set(commits)), 3)
        for parent, child in zip(commits, commits[1:]):
            self.assertEqual(self.git('rev-parse', child + '^'), parent)
        self.assertEqual(self.git('rev-parse', 'origin/001-team-1'), commits[-1])
        prompts = (self.root / 'prompts').read_text()
        self.assertEqual(prompts.count('No measurements, probes, benchmarks or full test-gate runs'), 3)


    def test_tasks_timeout_resumes_only_tasks(self):
        self.worker('tasks', expect=124)
        commit = self.checkpoint()['phases']['plan']['commit']
        proc = self.worker()
        self.assertIn('Resetting spec worker to committed checkpoint ' + commit, proc.stdout)
        self.assertEqual(self.calls(), ['specify', 'plan', 'tasks', 'tasks', 'digest'])
        self.assertNotIn('partial phase edits', (self.wt/'specs/001-team-1/tasks.md').read_text())


    def test_ticket_and_review_feedback_changes_invalidate_completed_prefix(self):
        for field in ('title', 'description', 'feedback'):
            with self.subTest(field=field):
                self.worker('tasks', expect=124)
                if field == 'feedback': self.write('comments.json', '[{"body":"Spec review FAIL: repair governance"}]')
                else:
                    value = json.loads((self.root / 'detail.json').read_text()); value[field] += ' changed'
                    self.write('detail.json', json.dumps(value))
                self.write('calls', ''); self.worker('tasks', expect=124)
                self.assertEqual(self.calls(), ['specify', 'plan', 'tasks'])

    def test_uncommitted_leftovers_are_discarded_by_reset(self):
        self.worker('tasks', expect=124)
        directory = self.wt / 'specs/001-team-1'
        (directory/'spec.md').write_text('uncommitted tracked corruption')
        (directory/'plan.md').write_text('staged corruption')
        self.git('add', 'specs', repo=self.wt)
        (directory/'leftover.md').write_text('untracked phase work')
        (self.wt/'unrelated.txt').write_text('untracked outside the feature')
        (self.wt/'logs').mkdir(); (self.wt/'logs/ignored').write_text('ignored phase work')
        self.write('calls', '')
        self.worker()
        self.assertEqual(self.calls(), ['tasks', 'digest'])
        self.assertEqual((directory/'spec.md').read_text(), VALID)
        self.assertEqual((directory/'plan.md').read_text(), '# Plan\n')
        for path in (directory/'leftover.md', self.wt/'unrelated.txt', self.wt/'logs/ignored'):
            self.assertFalse(path.exists(), str(path))
        self.assertEqual(self.git('status','--porcelain',repo=self.wt),'')


    def test_unrelated_preexisting_artifacts_never_qualify(self):
        decoy = self.repo / 'specs/009-other-ticket'; decoy.mkdir(parents=True)
        for name in ('spec.md', 'plan.md', 'tasks.md'): (decoy / name).write_text('stale other ticket')
        self.git('add', 'specs'); self.git('commit', '-qm', 'other ticket artifacts'); self.git('push', '-q', 'origin', 'main')
        self.worker()
        self.assertEqual(self.calls(), ['specify', 'plan', 'tasks', 'digest'])
        self.assertNotIn(str(decoy), self.checkpoint()['spec_directory'])
        self.assertIn('010-team-1', self.checkpoint()['spec_directory'])
        self.assertEqual((self.wt / 'specs/009-other-ticket/plan.md').read_text(), 'stale other ticket')

    def test_unregistered_or_foreign_worker_is_never_reused(self):
        self.worker('plan', expect=124)
        key = hashlib.sha256(str(self.wt).encode()).hexdigest()
        registration = self.repo / '.git/bureau/workers' / key
        self.assertTrue(registration.is_file())
        original = registration.read_text()
        registration.write_text(str(self.repo/'.git'))
        self.worker(expect=21)
        registration.write_text(original); registration.unlink()
        self.worker(expect=21)
        self.assertIn('partial phase edits', (self.wt/'specs/001-team-1/plan.md').read_text())
        self.assertEqual(self.calls(), ['specify', 'plan'])


    def test_changed_reservation_starts_fresh_on_new_reserved_branch(self):
        self.worker('plan', expect=124)
        (self.repo/'.git/bureau/spec-numbers.json').write_text('{"TEAM-1":2}')
        self.write('calls', '')
        self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])
        self.assertEqual(self.checkpoint()['branch'], '002-team-1')
        self.assertNotIn('specs/001-team-1/spec.md', self.git('ls-tree', '-r', '--name-only', 'HEAD', repo=self.wt))


    def inject_evidence_error(self, operation):
        path=self.repo/'scripts/bureau-evidence.py'
        with path.open('a') as out:
            if operation=='read':
                out.write("\n_real_read=read\ndef read(path, default=None):\n    if 'spec-checkpoints' in path.parts: raise OSError('injected checkpoint read I/O')\n    return _real_read(path,default)\n")
            elif operation=='manifest':
                out.write("\n_real_begin=Store.begin_run\ndef _fail_begin(self,*args,**kwargs):\n    raise OSError('injected manifest I/O')\nStore.begin_run=_fail_begin\n")
            else:
                out.write("\n_real_atomic=atomic\ndef atomic(path,value):\n    if 'spec-checkpoints' in path.parts: raise OSError('injected checkpoint write I/O')\n    return _real_atomic(path,value)\n")

    def test_checkpoint_write_errors_run_fresh_and_review_fail_still_routes(self):
        self.inject_evidence_error('write')
        self.write('spec-body','# No governance\n')
        proc=self.worker()
        self.assertIn('running all phases without reuse',proc.stderr)
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])
        self.worker(review=True)
        self.assertEqual((self.root/'state').read_text(),'Spec')
        self.assertTrue(any('Spec review: **FAIL**' in c['body'] for c in json.loads((self.root/'comments.json').read_text())))

    def test_missing_tasks_routes_to_spec_even_when_invalidation_fails(self):
        self.worker()
        self.git('rm','specs/001-team-1/tasks.md',repo=self.wt)
        self.git('commit','-qm','remove tasks for recovery test',repo=self.wt)
        self.git('push','-q','origin','HEAD:refs/heads/001-team-1',repo=self.wt)
        self.inject_evidence_error('write')
        proc=self.worker(review=True,expect=13)
        self.assertIn('checkpoint invalidation unavailable; missing tasks still require re-work',proc.stderr)
        self.assertEqual((self.root/'state').read_text(),'Spec')
        comments=json.loads((self.root/'comments.json').read_text())
        self.assertTrue(any('no tasks.md found' in c['body'] and 'Moving back to Spec' in c['body'] for c in comments))
        self.assertFalse(any('PASSED' in c['body'] for c in comments))
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])


    def test_artifact_contract_error_remains_fatal_despite_best_effort_storage(self):
        self.write('spec-body','')
        self.worker(expect=24)
        self.assertEqual(self.calls(),['specify'])
        self.assertEqual((self.root/'state').read_text(),'Triage')

    def test_phase_completion_io_error_disables_later_checkpoint_reuse(self):
        path=self.repo/'scripts/bureau-evidence.py'
        with path.open('a') as out:
            out.write("\n_real_atomic=atomic\ndef atomic(path,value):\n    if 'spec-checkpoints' in path.parts and 'specify' in value.get('phases',{}): raise OSError('injected completion I/O')\n    return _real_atomic(path,value)\n")
        self.worker()
        self.assertTrue((self.repo/'.git/bureau/spec-checkpoints-disabled/TEAM-1.json').is_file())
        self.assertFalse(self.checkpoint()['phases'])
        self.write('state','Triage'); self.write('calls','')
        self.worker()
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])

    def test_checkpoint_read_error_after_timeout_runs_fresh(self):
        self.worker('plan', expect=124)
        self.inject_evidence_error('read')
        self.write('calls', '')
        self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])
        self.assertFalse((self.root/'retained-plan').exists())


    def test_review_invalidation_io_error_cannot_publish_pass_or_skip_rework(self):
        self.worker()
        self.inject_evidence_error('write')
        self.write('review.json',json.dumps(dict(review_status='FAIL',ui_work_needed=False,summary='Repair requirements')))
        self.worker(review=True)
        self.assertEqual((self.root/'state').read_text(),'Spec')
        comments=json.loads((self.root/'comments.json').read_text())
        self.assertTrue(any('Spec review: **FAIL**' in c['body'] for c in comments))
        self.write('state','Triage'); self.write('calls','')
        self.worker()
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])

    def test_model_rejects_structurally_valid_but_inadequate_rationale(self):
        body=VALID.replace('This documentation change handles no credentials or personal data.',
                           'Required because governance is required')
        self.write('spec-body',body)
        self.worker()
        self.assertIsNone(r.s.governance_error(self.wt/'specs/001-team-1/spec.md'))
        self.write('review.json',json.dumps(dict(review_status='FAIL',ui_work_needed=False,
                   remaining_issues=['Circular rationale gives no applicability basis'],summary='Governance rationale is circular and vague')))
        self.worker(review=True)
        prompt=(self.root/'prompts').read_text()
        self.assertIn('You must judge substantive adequacy',prompt)
        self.assertIn('circular reasoning, vague assertions',prompt)
        self.assertIn('even when the deterministic check passes',prompt)
        self.assertEqual((self.root/'state').read_text(),'Spec')
        self.assertEqual(self.checkpoint()['phases'],{})
        comments=json.loads((self.root/'comments.json').read_text())
        self.assertTrue(any('Spec review: **FAIL**' in c['body'] and 'circular and vague' in c['body'] for c in comments))
        self.write('state','Triage'); self.write('calls',''); self.worker()
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])

    def test_checkpoint_read_error_uses_normal_reset_authorization(self):
        self.worker()
        self.inject_evidence_error('read')
        self.write('state','Triage'); self.write('calls','')
        self.worker()
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])

    def test_manifest_io_error_disables_reuse_but_allows_owned_fresh_run(self):
        self.inject_evidence_error('manifest')
        proc=self.worker()
        self.assertIn('bureau evidence: WARN:',proc.stderr)
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])

    def test_manifest_io_failure_revokes_old_checkpoints_after_storage_recovers(self):
        self.worker('tasks',expect=124)
        evidence = self.repo/'scripts/bureau-evidence.py'
        original = evidence.read_text()
        self.inject_evidence_error('manifest')
        self.worker('plan',expect=124)
        evidence.write_text(original)
        self.write('calls',''); self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])

    def test_large_published_research_is_streamed_and_changes_invalidate_specify(self):
        research='<!-- bureau-research: api -->\nAPI reference: '+('x'*(1024*1024))
        self.write('comments.json',json.dumps([dict(body=research)]))
        self.worker('tasks',expect=124)
        first=self.checkpoint()['input_sha256']
        self.write('comments.json',json.dumps([dict(body=research+' changed endpoint')]))
        self.write('calls','')
        self.worker('tasks',expect=124)
        self.assertEqual(self.calls(),['specify','plan','tasks'])
        self.assertNotEqual(self.checkpoint()['input_sha256'],first)

    def test_new_research_invalidates_specify_but_publication_does_not(self):
        config=json.loads((self.repo/'.bureau.json').read_text()); config['agents']['research']=True
        self.write_config(config)
        detail=json.loads((self.root/'detail.json').read_text()); detail['labels']=['needs-research']
        self.write('detail.json',json.dumps(detail))
        self.write('research','<!-- bureau-research: api -->\nCurrent API v1 uses endpoint /v1.')
        with (self.repo/'scripts/bureau-config.sh').open('a') as out:
            out.write(r'''
_original_stage=$(declare -f run_stage_for)
eval "${_original_stage/run_stage_for/run_without_research}"
run_stage_for() {
  if [ "$1" = research ]; then cat "$SPEC_TEST_ROOT/research"
  else run_without_research "$@"; fi
}
remove_issue_label() { :; }
''')
        self.worker('tasks',expect=124)
        first=self.checkpoint()['input_sha256']
        detail['labels']=[]; self.write('detail.json',json.dumps(detail)); self.write('calls','')
        self.worker('tasks',expect=124)
        self.assertEqual(self.calls(),['tasks'])
        self.assertEqual(self.checkpoint()['input_sha256'],first)
        self.write('research','<!-- bureau-research: api -->\nCurrent API v2 requires endpoint /v2 and a new credential.')
        detail['labels']=['needs-research']; self.write('detail.json',json.dumps(detail)); self.write('calls','')
        self.worker('tasks',expect=124)
        self.assertEqual(self.calls(),['specify','plan','tasks'])
        self.assertNotEqual(self.checkpoint()['input_sha256'],first)


    def test_failed_governance_review_cannot_skip_specify_on_retry(self):
        self.write('spec-body', '# No governance section\n')
        self.worker(); self.worker(review=True)
        self.assertEqual((self.root / 'state').read_text(), 'Spec')
        self.assertEqual(self.checkpoint()['phases'], {})
        self.assertEqual(self.checkpoint()['invalidation']['from_phase'], 'specify')
        self.write('state', 'Triage'); self.write('spec-body', VALID); self.write('calls', '')
        self.worker()
        self.assertEqual(self.calls(), ['specify', 'plan', 'tasks', 'digest'])

    def test_model_pass_cannot_bypass_governance_validator(self):
        self.worker()
        for body in ('# Missing\n', '## Governance and security\n', '## Governance and security\nTBD\n', VALID):
            with self.subTest(body=body):
                # Review reads the real remote branch, not an uncommitted fixture.
                file = self.wt / 'specs/001-team-1/spec.md'; file.write_text(body)
                self.git('checkout', '-q', '001-team-1', repo=self.wt)
                self.git('add', 'specs', repo=self.wt); self.git('commit', '-qm', 'governance fixture', repo=self.wt)
                self.git('push', '-q', 'origin', 'HEAD', repo=self.wt)
                self.git('checkout', '-q', '--detach', repo=self.wt)
                self.write('state', 'Spec Review'); self.worker(review=True)
                self.assertEqual((self.root / 'state').read_text(), 'Build' if body == VALID else 'Spec')

    def test_specify_and_plan_prompts_and_shared_instructions_forbid_measurement(self):
        self.worker()
        for name in ('bureau-stage.md', '../instructions/workflow.md'):
            self.assertIn('no measurements, probes, benchmarks or full test-gate runs', (SCRIPTS / name).read_text())


    def test_plan_checkpoint_includes_successful_edits_to_specify_artifacts(self):
        path = self.repo/'scripts/bureau-config.sh'
        with path.open('a') as out:
            out.write('''
eval "$(declare -f run_stage_for | sed '1s/run_stage_for/run_original/')"
run_stage_for() {
  run_original "$@" || return $?
  if [ "${BUREAU_PROVIDER_PHASE:-}" = plan ]; then
    printf '\nPlan clarification\n' >> "$(jq -r .feature_directory .specify/feature.json)/spec.md"
  fi
}
''')
        self.worker('tasks',expect=124)
        saved = self.checkpoint()['phases']['plan']
        self.assertIn('spec.md', saved['artifacts'])
        self.write('calls',''); self.worker()
        self.assertEqual(self.calls(), ['tasks','digest'])
        self.assertIn('Plan clarification',(self.wt/'specs/001-team-1/spec.md').read_text())

    def test_remote_changes_require_fresh_phases_without_rewriting_history(self):
        self.worker()
        self.git('checkout','-q','001-team-1',repo=self.wt)
        (self.wt/'specs/001-team-1/spec.md').write_text(VALID + '\nReview clarification\n')
        self.git('add','specs',repo=self.wt); self.git('commit','-qm','review clarification',repo=self.wt)
        published = self.git('rev-parse','HEAD',repo=self.wt)
        self.git('push','-q','origin','HEAD',repo=self.wt); self.git('checkout','-q','--detach',repo=self.wt)
        self.write('state','Triage'); self.write('calls',''); self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])
        self.git('merge-base','--is-ancestor',published,'origin/001-team-1')

    def test_corrupt_checkpoint_runs_fresh_and_is_replaced(self):
        self.worker('plan',expect=124)
        (self.repo/'.git/bureau/evidence/v1/spec-checkpoints/TEAM-1.json').write_text('{broken json')
        self.write('calls',''); self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])
        self.assertEqual(self.checkpoint()['checkpoint_version'], 2)

    def test_app_adoption_only_revokes_disposable_ownership(self):
        self.worker('plan',expect=124)
        key = hashlib.sha256(str(self.wt).encode()).hexdigest()
        owned = self.repo/'.git/bureau'
        self.write('state','Triage')
        result = subprocess.run([sys.executable,str(self.repo/'scripts/bureau-runtime.py'),'--repo',str(self.wt),
                                 'prepare','TEAM-1','spec','--owner','app-test'],env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = json.loads(result.stdout)['run_id']
        self.assertFalse((owned/'workers'/key).exists())
        self.assertFalse((owned/'preserved'/(key+'.json')).exists())
        subprocess.run([sys.executable,str(self.repo/'scripts/bureau-runtime.py'),'--repo',str(self.wt),
                        'release',run],env=self.env,check=True,capture_output=True)
        self.worker(expect=21)
        self.assertEqual(self.calls(), ['specify','plan'])

    def test_tampered_commit_cannot_supply_checkpoint_artifacts(self):
        self.worker('plan', expect=124)
        directory = self.wt/'specs/001-team-1'
        self.git('checkout','-q','001-team-1',repo=self.wt)
        (directory/'spec.md').write_text(VALID + '\nTampered committed requirement\n')
        self.git('add','specs',repo=self.wt)
        self.git('commit','-qm','tamper checkpoint commit',repo=self.wt)
        value = self.checkpoint()
        value['phases']['specify']['commit'] = self.git('rev-parse','HEAD',repo=self.wt)
        (self.repo/'.git/bureau/evidence/v1/spec-checkpoints/TEAM-1.json').write_text(json.dumps(value))
        self.git('checkout','-q','--detach',repo=self.wt)
        self.write('calls','')
        self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])
        self.assertEqual((directory/'spec.md').read_text(), VALID)

    def test_changed_feedback_requires_fresh_specify(self):
        self.worker('plan',expect=124)
        self.write('comments.json','[{"body":"New feedback: require a different API"}]')
        self.write('calls','')
        self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])

    def test_origin_checkpoint_survives_new_worker_and_missing_local_branch(self):
        self.worker()
        value = self.checkpoint()
        self.git('branch','-D','001-team-1')
        self.wt = self.root/'replacement worker'
        self.write('state','Triage'); self.write('calls','')
        proc = self.worker()
        self.assertIn(value['phases']['tasks']['commit'], proc.stdout)
        self.assertEqual(self.calls(), ['digest'])

    def test_old_checkpoint_schema_runs_fresh(self):
        self.worker('plan',expect=124)
        value = self.checkpoint(); value.pop('checkpoint_version')
        (self.repo/'.git/bureau/evidence/v1/spec-checkpoints/TEAM-1.json').write_text(json.dumps(value))
        self.write('calls',''); self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])

    def advance_remote_feature(self):
        other=self.root/'another clone'
        self.git('clone','-q','--branch','001-team-1',str(self.root/'origin.git'),str(other))
        self.git(*git_maintenance.OFF_CONFIG,repo=other)
        self.git('config','user.name','Other maintainer',repo=other)
        self.git('config','user.email','other@invalid',repo=other)
        (other/'specs/001-team-1/clarification.md').write_text('Remote review clarification\n')
        self.git('add','.',repo=other); self.git('commit','-qm','remote review clarification',repo=other)
        self.git('push','-q','origin','HEAD',repo=other)
        return self.git('rev-parse','HEAD',repo=other)

    def test_remote_feature_advance_fast_forwards_stale_local_on_one_retry(self):
        self.worker()
        previous=self.git('rev-parse','001-team-1')
        remote=self.advance_remote_feature()
        self.assertEqual(self.git('rev-parse','001-team-1'),previous,'another clone moved the local branch')
        self.write('state','Triage'); self.write('calls','')
        self.worker()  # A single retry must push successfully without a force push.
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])
        self.assertEqual((self.wt/'specs/001-team-1/clarification.md').read_text(),'Remote review clarification\n')
        first=self.checkpoint()['phases']['specify']['commit']
        self.assertEqual(self.git('rev-parse',first+'^'),remote)
        self.assertEqual(self.git('rev-parse','001-team-1'),self.git('rev-parse','origin/001-team-1'))
        self.assertEqual((self.root/'state').read_text(),'Spec Review')

    def test_diverged_feature_halts_once_needs_human_before_provider(self):
        self.worker()
        remote=self.advance_remote_feature()
        self.git('switch','-q','001-team-1',repo=self.wt)
        (self.wt/'specs/001-team-1/local-note.md').write_text('Local completed work\n')
        self.git('add','.',repo=self.wt); self.git('commit','-qm','local-only note',repo=self.wt)
        local=self.git('rev-parse','HEAD',repo=self.wt)
        self.git('checkout','-q','--detach',repo=self.wt)
        self.write('state','Triage'); self.write('calls','')
        proc=self.worker(expect=25)
        self.assertEqual(self.calls(),[],'divergence spent provider work')
        self.assertIn('ERROR: needs-human:',proc.stderr)
        self.assertIn('must reconcile both histories',proc.stderr)
        self.assertIn('TEAM-1\tneeds-human',(self.root/'labels').read_text())
        comments=json.loads((self.root/'comments.json').read_text())
        self.assertTrue(any('have diverged' in value['body'] for value in comments))
        self.assertEqual(self.git('rev-parse','001-team-1'),local)
        self.assertEqual(self.git('rev-parse','origin/001-team-1'),remote)
        self.assertEqual((self.root/'state').read_text(),'Triage')

    def test_late_held_feature_halts_and_actual_picker_skips_ticket(self):
        # Reserve first, then put that exact branch in a user's checkout. No
        # checkpoint exists, so reset succeeds and the pipeline hits the late
        # ownership check, after the worker cleared its reset-refusal trap.
        subprocess.run([sys.executable,str(self.repo/'scripts/bureau-supervision.py'),
                        '--repo',str(self.repo),'reserve-spec-number','TEAM-1'],check=True,capture_output=True)
        holder=self.root/'user checkout'
        self.git('worktree','add','-q','-b','001-team-1',str(holder),'main')
        (holder/'user-note.txt').write_text('Uncommitted user work must survive\n')
        before=(self.git('rev-parse','HEAD',repo=holder),self.git('status','--porcelain',repo=holder))
        with (self.repo/'scripts/bureau-config.sh').open('a') as out:
            out.write(r'''
# Fake only Linear responses; pipeline_pick_next and pick_issue stay shipped.
add_issue_label() {
  [ "${SPEC_FAIL_LABEL:-0}" != 1 ] || return 27
  printf '%s\t%s\n' "$1" "$2" >> "$SPEC_TEST_ROOT/labels"
}
linear_raw() {
  local human=false
  if [ -f "$SPEC_TEST_ROOT/labels" ] && grep -q 'needs-human' "$SPEC_TEST_ROOT/labels"; then human=true; fi
  jq -n --argjson human "$human" '{data:{issues:{nodes:[
    {identifier:"TEAM-1",priority:1,createdAt:"2026-01-01",labels:{nodes:([{name:"lane-2"}]+(if $human then [{name:"needs-human"}] else [] end))},inverseRelations:{nodes:[]}},
    {identifier:"TEAM-2",priority:2,createdAt:"2026-01-02",labels:{nodes:[{name:"lane-2"}]},inverseRelations:{nodes:[]}}
  ]}}}'
}
''')
        def pick():
            proc=subprocess.run(['bash','-c','source "$1"; pipeline_pick_next spec-pipeline.sh',
                                 'test',str(self.repo/'scripts/bureau-config.sh')],
                                cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=15)
            self.assertEqual(proc.returncode,0,proc.stderr)
            return proc.stdout.strip()
        for fails in (False,True):
            with self.subTest(label_write_fails=fails):
                self.env['SPEC_FAIL_LABEL']='1' if fails else '0'
                (self.root/'labels').unlink(missing_ok=True)
                self.assertEqual(pick(),'TEAM-1')
                self.worker(expect=21)
                self.assertEqual((self.root/'state').read_text(),'Triage')
                self.assertEqual(pick(),'TEAM-2','ownership refusal was selected again by the real picker')
                self.assertFalse((self.root/'calls').exists(),'ownership refusal spent provider work')
                self.assertEqual(self.git('branch','--show-current',repo=holder),'001-team-1')
                self.assertEqual(before,(self.git('rev-parse','HEAD',repo=holder),self.git('status','--porcelain',repo=holder)))
                self.assertEqual((holder/'user-note.txt').read_text(),'Uncommitted user work must survive\n')
                if fails:
                    self.assertTrue((self.repo/'.git/bureau/needs-human-held/TEAM-1').is_file())
                else:
                    self.assertIn('TEAM-1\tneeds-human',(self.root/'labels').read_text())
                comments=json.loads((self.root/'comments.json').read_text())
                self.assertTrue(any('ownership-conflict' in entry['body'] and str(holder) in entry['body'] for entry in comments))

    def test_missing_local_feature_starts_fetched_tip_on_fresh_retry(self):
        self.worker()
        remote=self.advance_remote_feature()
        (self.repo/'new-main-note.md').write_text('Main advanced independently\n')
        self.git('add','.'); self.git('commit','-qm','new main history')
        self.git('push','-q','origin','main')
        self.git('branch','-D','001-team-1')
        self.write('state','Triage'); self.write('calls','')
        self.worker()
        self.assertEqual(self.calls(),['specify','plan','tasks','digest'])
        first=self.checkpoint()['phases']['specify']['commit']
        self.assertEqual(self.git('rev-parse',first+'^'),remote,'fresh feature started from the newer base instead of origin feature')
        self.assertEqual(self.git('rev-parse','001-team-1'),self.git('rev-parse','origin/001-team-1'))

    def test_unreachable_checkpoint_runs_fresh(self):
        self.worker('plan',expect=124)
        self.git('branch','-f','001-team-1','origin/main')
        self.write('calls',''); self.worker()
        self.assertEqual(self.calls(), ['specify','plan','tasks','digest'])

    def test_phase_commits_exclude_provider_staged_unrelated_changes(self):
        path = self.repo/'scripts/bureau-config.sh'
        with path.open('a') as out:
            out.write('''
eval "$(declare -f run_stage_for | sed '1s/run_stage_for/run_original/')"
run_stage_for() {
  run_original "$@" || return $?
  if [ "${BUREAU_PROVIDER_PHASE:-}" = specify ]; then
    printf 'provider unrelated edit' > unrelated-provider.txt
    git add unrelated-provider.txt
  fi
}
''')
        self.worker('plan',expect=124)
        value = self.checkpoint()
        self.assertNotIn('unrelated-provider.txt', self.git('ls-tree','-r','--name-only',value['phases']['specify']['commit']))


if __name__ == '__main__': unittest.main()
