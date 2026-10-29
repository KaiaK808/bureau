"""Full structured merge and pinned-anchor checks; fake providers only."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import supervision_pipeline_test as fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_TEST_REVIEW_SCRIPTS', ROOT / 'templates/scripts')).resolve()


def load(name):
    spec = importlib.util.spec_from_file_location('merge_test_' + name, SCRIPTS / ('bureau-' + name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def finding(message='Real defect', severity='BUG', **fields):
    return dict(dict(file='kept.txt', line=1, **{'class': severity}, msg=message), **fields)


def specialist(role, findings=(), **fields):
    counts = {s.lower(): sum(f['class'] == s for f in findings) for s in ('CRITICAL', 'BUG', 'MINOR', 'SKIP')}
    return dict(specialist=role, counts=counts, findings=list(findings), summary=role + ' summary', **fields)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.m = load('review')
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-review-merge-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.git('init', '-qb', 'main'); self.git('config', 'user.name', 'Test'); self.git('config', 'user.email', 'test@bureau')
        (self.repo / 'kept.txt').write_bytes(b'first\r\nsecond\nthird')
        (self.repo / 'deleted.txt').write_bytes(b'deleted base code\n')
        self.git('add', '.'); self.git('commit', '-qm', 'test: base')
        self.base = self.git('rev-parse', 'HEAD')
        (self.repo / 'deleted.txt').unlink()
        (self.repo / 'SPEC.md').write_text('Relevant contract.\n')
        self.git('add', '.'); self.git('commit', '-qm', 'test: head')
        self.head = self.git('rev-parse', 'HEAD')
        self.outputs = self.root / 'outputs'; self.outputs.mkdir()
        self.values = {r: specialist(r) for r in self.m.ROLES}
        self.patch = patch.object(self.m, 'git', lambda *a: subprocess.check_output(['git', '-C', str(self.repo), *a], stderr=subprocess.DEVNULL))
        self.patch.start(); self.addCleanup(self.patch.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.STDOUT, text=True).strip()

    def anchor(self, path='kept.txt', side='head', start=1, end=1, scope=None):
        commit = self.head if side == 'head' else self.base
        oid = self.git('rev-parse', commit + ':' + path)
        data = subprocess.check_output(['git', '-C', str(self.repo), 'cat-file', 'blob', oid])
        if scope:
            return dict(side=side, path=path, blob=oid, scope=scope, sha256=hashlib.sha256(data).hexdigest())
        cited = b''.join(data.splitlines(keepends=True)[start - 1:end])
        return dict(side=side, path=path, blob=oid, start_line=start, end_line=end, sha256=hashlib.sha256(cited).hexdigest())

    def merge(self, refs=None):
        for r, value in self.values.items():
            (self.outputs / (r + '.txt')).write_text('Author reasoning stays private.\n```json\n' + json.dumps(value) + '\n```\n')
        return self.m.merge(self.outputs, self.base, self.head, refs)

    def test_each_specialist_is_preserved_and_template_is_stable(self):
        for r in self.values:
            self.values[r] = specialist(r, [finding(r + ' claim')])
        result, findings, metadata = self.merge()
        self.assertEqual(len(findings), 3)
        self.assertEqual({f['msg'] for f in findings}, {r + ' claim' for r in self.values})
        self.assertEqual(result['bugs'], 3); self.assertEqual(result['security_issues'], 1)
        self.assertEqual(metadata['mode'], 'deterministic')
        expected = (ROOT / 'tests/fixtures/review-merge/comment.md').read_text().rstrip('\n')
        self.assertEqual(result['comment'], expected)
        self.assertEqual(self.m.encoded(result), self.m.encoded(self.merge()[0]))

    def test_duplicates_take_highest_severity_union_detail_and_adr_refs(self):
        self.values['correctness'] = specialist('correctness', [finding('First detail', 'MINOR', title=' Race — CONDITION ', line_end=2, adr_refs=['0001'])])
        self.values['security'] = specialist('security', [finding('Security detail', 'CRITICAL', title='race condition', line_end=2, adr_refs=['0003'])])
        self.values['performance'] = specialist('performance', [finding('Performance detail', title='RACE: condition', line_end=2)])
        result, findings, metadata = self.merge(['0001', '0003'])
        self.assertEqual(len(findings), 1); self.assertEqual(findings[0]['class'], 'CRITICAL')
        self.assertEqual(findings[0]['adr_refs'], ['0001', '0003'])
        self.assertEqual(findings[0]['specialists'], sorted(self.values))
        self.assertEqual(result['findings'][0]['finding_id'], findings[0]['finding_id'])
        self.assertEqual(result['findings'][0]['line_end'], 2)
        self.assertIn('First detail', result['comment']); self.assertIn('Security detail', result['comment'])
        self.assertIn('Performance detail', result['comment']); self.assertEqual(metadata['critical_floor'], 1)
        self.assertEqual(result['bugs'], 0)

    def test_different_ranges_and_titles_do_not_collapse(self):
        self.values['correctness'] = specialist('correctness', [finding(), finding(line_end=2), finding('Different title')])
        self.assertEqual(len(self.merge()[1]), 3)

    def test_malformed_missing_wrong_role_and_schema_fields_fall_back(self):
        invalid = [None, {}, specialist('security'), specialist('correctness', [dict(file='a', line=True, **{'class':'BUG'}, msg='bad')])]
        for value in invalid:
            self.values['correctness'] = value
            result, _, metadata = self.merge()
            self.assertIsNone(result); self.assertEqual(metadata['malformed_specialists'], ['correctness'])
        self.values['correctness'] = specialist('correctness'); self.merge()
        (self.outputs / 'performance.txt').unlink()
        result, _, metadata = self.m.merge(self.outputs, self.base, self.head, None)
        self.assertIsNone(result); self.assertEqual(metadata['malformed_specialists'], ['performance'])

    def test_count_only_critical_security_and_acceptance_floors(self):
        self.values['security']['counts']['critical'] = 1
        self.values['correctness']['acceptance_gaps'] = ['Missing required behavior']
        result, _, metadata = self.merge()
        self.assertEqual(metadata['critical_floor'], 1); self.assertEqual(result['security_issues'], 1)
        self.assertEqual(result['missing_acceptance'], ['Missing required behavior'])

    def test_fallback_cannot_drop_readable_specialist_floors(self):
        self.values['performance'] = 'malformed'
        self.values['security'] = specialist('security', [finding('Security defect', 'CRITICAL', anchor={**self.anchor(), 'blob':'f' * 40})])
        result, _, metadata = self.merge()
        self.assertIsNone(result)
        self.assertEqual((metadata['critical_floor'], metadata['security_floor'], metadata['invalid_anchors']), (1, 1, 1))
        # Even a different schema fault must not erase an identified
        # specialist's explicit CRITICAL claim during model fallback.
        self.values = {r: specialist(r) for r in self.m.ROLES}
        self.values['correctness'] = specialist('correctness', [finding('Critical claim', 'CRITICAL')])
        self.values['correctness']['counts']['bug'] = 1
        result, _, metadata = self.merge()
        self.assertIsNone(result); self.assertEqual(metadata['critical_floor'], 1)

    def test_anchor_integrity_and_valid_deleted_base_line(self):
        valid = finding(anchor=self.anchor())
        self.assertEqual(self.m.anchor_check(valid, self.base, self.head), ('valid', b'first\r\n'))
        deleted = finding(file='deleted.txt', anchor=self.anchor('deleted.txt', 'base'))
        self.assertEqual(self.m.anchor_check(deleted, self.base, self.head), ('valid', b'deleted base code\n'))
        for key, bad, reason in [('path', 'absent.txt', 'path'), ('end_line', 999, 'range'),
                                 ('blob', self.git('rev-parse', self.base + ':deleted.txt'), 'foreign_blob'), ('sha256', '0' * 64, 'stale_digest'),
                                 ('side', 'other', 'side')]:
            changed = copy.deepcopy(valid); changed['anchor'][key] = bad
            self.assertEqual(self.m.anchor_check(changed, self.base, self.head)[0], reason)
        missing = finding(file='absent.txt', anchor={**self.anchor(), 'path':'absent.txt'})
        self.assertEqual(self.m.anchor_check(missing, self.base, self.head)[0], 'absent')
        foreign = finding(anchor={**self.anchor('deleted.txt', 'base'), 'path':'kept.txt'})
        self.assertEqual(self.m.anchor_check(foreign, self.base, self.head)[0], 'foreign_blob')
        # The checkout is irrelevant; all reads stay on pinned blobs.
        (self.repo / 'kept.txt').write_text('live edit\n')
        self.assertEqual(self.m.anchor_check(valid, self.base, self.head)[0], 'valid')

    def test_grounding_metadata_cannot_hide_invalid_anchor(self):
        self.values['correctness'] = specialist('correctness', [finding(anchor={**self.anchor(), 'blob':'f' * 40})])
        _, _, metadata = self.merge()
        self.assertEqual(metadata['invalid_anchors'], 1)
        self.assertEqual(metadata['anchor_checks'][0]['status'], 'foreign_blob')

    def test_malformed_anchor_schema_cannot_disappear_in_fallback(self):
        for anchor in ({'side':'other'}, [], None):
            self.values['correctness'] = specialist('correctness', [finding(anchor=anchor)])
            result, _, metadata = self.merge()
            self.assertIsNone(result)
            self.assertEqual(metadata['invalid_anchors'], 1)

    def test_line_zero_requires_explicit_file_scope(self):
        scoped = finding(line=0, anchor=self.anchor(scope='file'))
        self.assertEqual(self.m.anchor_check(scoped, self.base, self.head)[0], 'valid')
        scoped['line'] = 1
        self.assertEqual(self.m.anchor_check(scoped, self.base, self.head)[0], 'range')
        self.assertEqual(self.m.anchor_check(finding(line=0), self.base, self.head)[0], 'missing')

    def test_settings_and_policy_defaults_doctor(self):
        self.assertEqual(self.m.settings({}), ('off', False))
        for agents in ({'review_grounding':'optional'}, {'review_grounding':True}, {'finding_verifier':True},
                       {'finding_verifier':{'enabled':'false'}}, {'finding_verifier':{'enabled':True, 'mode':'enforced'}}):
            with self.assertRaises(ValueError): self.m.settings(dict(agents=agents))
        config = dict(linear=dict(teams=[dict(id='team', key='T', states=dict(build='build'))]), agents={}, repo={})
        doctor = load('doctor'); self.assertEqual(doctor.validate(config), [])
        config['agents']['review_grounding'] = 'bad'
        self.assertTrue(any('review_grounding' in e for e in doctor.validate(config)))

    def test_verifier_inputs_exclude_reasoning_and_are_bounded(self):
        self.values['correctness'] = specialist('correctness', [finding('Claim ' + str(i), anchor=self.anchor()) for i in range(10)])
        result, findings, metadata = self.merge()
        self.m.prepare_verifier(self.outputs, findings, self.base, self.head, ['SPEC.md'], metadata)
        self.assertEqual(metadata['verifier_omitted'], 2)
        prompts = list(self.outputs.glob('verifier-*.prompt')); self.assertEqual(len(prompts), 8)
        for path in prompts:
            data = path.read_text(); self.assertNotIn('Author reasoning', data); self.assertNotIn('correctness summary', data)
            self.assertIn('Relevant contract', data); self.assertIn('first\\r\\n', data)
            self.assertLessEqual(len(data.encode()), self.m.MAX_REQUEST + 600)
        raw = self.m.encoded(metadata)
        self.assertNotIn('Real defect', raw); self.assertNotIn('first', raw); load('evidence').review_merge_shape(metadata)

    def test_verifier_identity_and_outcome_validation_only_changes_metadata(self):
        self.values['correctness'] = specialist('correctness', [finding(anchor=self.anchor())])
        result, findings, metadata = self.merge()
        self.m.prepare_verifier(self.outputs, findings, self.base, self.head, [], metadata)
        metadata_path = self.outputs / 'merge-metadata.json'
        metadata_path.write_text(self.m.encoded(metadata))
        expected = metadata['verifier'][0]
        merged = self.outputs / 'merged.json'; merged.write_text(self.m.encoded(result)); before = merged.read_bytes()
        output = self.outputs / 'result.txt'
        for outcome, foreign, want in [('confirmed', False, 'confirmed'), ('refuted', False, 'refuted'),
                                        ('unverifiable', False, 'unverifiable'), ('confirmed', True, 'unverifiable'),
                                        ('invalid', False, 'unverifiable')]:
            value = dict(finding_id='f' * 64 if foreign else expected['finding_id'],
                         anchor_sha256=expected['anchor_sha256'], outcome=outcome)
            output.write_text(json.dumps(value))
            with patch.object(sys, 'argv', ['bureau-review.py', 'verifier-result', '--directory', str(self.outputs),
                                            '--request', str(self.outputs / 'verifier-0.prompt'), '--result', str(output)]):
                self.assertEqual(self.m.main(), 0)
            self.assertEqual(json.loads(metadata_path.read_text())['verifier'][0]['outcome'], want)
            self.assertEqual(merged.read_bytes(), before)

    def test_shadow_silence_does_not_create_a_human_hold(self):
        runner = re.search(r'^run_stage_for\(\) \{\n.*?^\}', (SCRIPTS / 'bureau-config.sh').read_text(), re.M | re.S).group(0)
        hold = self.root / 'hold'
        script = '''bureau_ticket_checkpoint() { :; }
bureau_without_secrets() { return 25; }
bureau_silence_hold() { : > "$HOLD_FILE"; }
''' + runner + '\nrun_stage_for code_review claim\n'
        for phase in ('verdict', 'finding_verifier'):
            hold.unlink(missing_ok=True)
            completed = subprocess.run(['/bin/bash', '-c', script], cwd=self.repo,
                                       env={**os.environ, 'BUREAU_PROVIDER_PHASE':phase, 'HOLD_FILE':str(hold),
                                            'BUREAU_CONFIG':'fixture.json', 'BUREAU_RUNTIME':str(SCRIPTS / 'bureau-runtime.py')},
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 25, completed.stderr)
            self.assertEqual(hold.exists(), phase == 'verdict')

    def test_missing_checker_receipt_keeps_aggregate_usage_unknown(self):
        evidence = load('evidence'); store = evidence.Store(self.root / 'evidence', clock=lambda:100)
        run, head = 'a' * 32, 'b' * 40
        store.begin_run(run, 'T-1', self.root, head)
        attempt = store.begin_attempt(run, 'T-1', 'code_review', 'stage', self.root, head)
        _, _, metadata = self.merge()
        metadata['verifier'] = [dict(finding_id='c' * 64, anchor_sha256='d' * 64,
                                     outcome='unverifiable', basis='invalid_result')]
        for index, phase in enumerate((*self.m.ROLES, 'finding_verifier'), 1):
            if phase == 'finding_verifier':
                partial = store.review_round(run, attempt, 1, 'BUG', merge=metadata)
                self.assertEqual(partial['calls'], 3)
                self.assertTrue(all(n is None for n in partial['usage'].values()))
            store.put_receipt(dict(run_id=run, issue='T-1', provider_attempt_id=f'{index:032x}',
                                   stage_attempt_id=attempt, stage='code_review', phase=phase, provider='codex',
                                   provider_exit=0, normalized_exit=0, outcome='complete', start_head=head, end_head=head,
                                   started_at=100, ended_at=100, provider_logs=str(self.root),
                                   usage=dict(input_tokens=1, output_tokens=1), total_cost_usd=0))
        complete = store.review_round(run, attempt, 1, 'BUG', merge=metadata)
        self.assertEqual(complete['calls'], 4); self.assertEqual(complete['usage']['input_tokens'], 4)


PROVIDER = r'''
import json, os, pathlib, sys
if sys.argv[1] == 'login': sys.exit(0)
if sys.argv[1] == 'auth': print(json.dumps(dict(loggedIn=True))); sys.exit(0)
if sys.argv[1] == '--help': print('Usage: claude'); sys.exit(0)
prompt = sys.stdin.read(); phase = os.environ.get('BUREAU_PROVIDER_PHASE')
capture = pathlib.Path(os.environ['REVIEW_CAPTURE']); capture.mkdir(exist_ok=True)
(capture / (phase + '.txt')).write_text(prompt)
with open(os.environ['MODEL_LOG'], 'a') as out: out.write(phase + '\n')
if phase == 'finding_verifier':
    request = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{'))
    result = dict(finding_id=request['finding_id'], anchor_sha256=request['anchor_sha256'], outcome='refuted')
else: result = json.loads(os.environ['REVIEW_RESULTS'])[phase]
if '-o' in sys.argv:
    pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text(json.dumps(result) if not isinstance(result, str) else result)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
else:
    print(json.dumps(dict(type='result', result=json.dumps(result), usage=dict(input_tokens=1,output_tokens=1))))
'''


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.source_temp = tempfile.TemporaryDirectory(prefix='bureau-review-source-'); self.addCleanup(self.source_temp.cleanup)
        source = Path(self.source_temp.name).resolve(); shutil.copytree(SCRIPTS, source / 'templates/scripts')
        with patch.object(fixture, 'ROOT', source), patch.object(fixture, 'CODEX', PROVIDER):
            self.f = fixture.SupervisionPipelineTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.repo, self.root = self.f.repo, self.f.root
        self.config = json.loads((self.repo / '.bureau.json').read_text()); self.config['repo']['test_command'] = 'true'
        self.values = {r: specialist(r) for r in ('correctness', 'security', 'performance')}
        self.values['verdict'] = dict(verdict='APPROVE', bugs=0, security_issues=0, missing_acceptance=[], fixes_needed=[],
                                      summary='Fallback summary', comment='Fallback prose', findings=[])
        self.workers = 0

    def claim(self, severity='MINOR', invalid=False):
        commit = self.f.git('rev-parse', 'origin/001-task')
        oid = self.f.git('rev-parse', commit + ':task1.txt')
        data = subprocess.check_output(['git', '-C', str(self.repo), 'cat-file', 'blob', oid])
        return finding('Pinned claim', severity, file='task1.txt', anchor=dict(side='head', path='task1.txt', blob='f' * 40 if invalid else oid,
                       start_line=1, end_line=1, sha256=hashlib.sha256(data).hexdigest()))

    def run_review(self, code):
        if self.workers:
            # A requested stop intentionally reuses its checkpoint. Explicitly
            # resume this fixture before asking for a new paid review round.
            subprocess.run([sys.executable, 'scripts/bureau-supervision.py', 'resume', 'T-1'],
                           cwd=self.repo, env=self.f.env, check=True, stdout=subprocess.DEVNULL)
            items = json.loads(self.f.linear.read_text())
            items['T-1']['state'] = dict(id='build_review', name='Build Review')
            items['T-1']['labels']['nodes'] = [v for v in items['T-1']['labels']['nodes'] if v['name'] != 'needs-human']
            self.f.linear.write_text(json.dumps(items))
        self.workers += 1
        (self.repo / '.bureau.json').write_text(json.dumps(self.config))
        capture = self.root / ('capture-' + str(self.workers)); capture.mkdir()
        completed = subprocess.run(['bash', 'scripts/bureau-worker.sh', 'T-1', 'code-review-pipeline.sh',
                                    str(self.root / ('worker-' + str(self.workers))), '001-task'], cwd=self.repo,
                                   env={**self.f.env, 'BUREAU_NO_MERGE':'1', 'BUREAU_STOP_REQUESTED':'1',
                                        'REVIEW_CAPTURE':str(capture), 'REVIEW_RESULTS':json.dumps(self.values)},
                                   capture_output=True, text=True, timeout=90)
        for name in re.findall(r'Specialist outputs preserved at ([^\n]+)', completed.stderr):
            path = Path(name).resolve()
            if path.parent == Path(tempfile.gettempdir()).resolve(): self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        self.assertEqual(completed.returncode, code, completed.stdout + completed.stderr)
        manifests = list((self.repo / '.git/bureau/evidence/v1/runs').glob('*/manifest.json'))
        records = [a['review_round'] for p in manifests for a in json.loads(p.read_text())['attempts'] if 'review_round' in a]
        self.assertTrue(records)
        record = max(records, key=lambda r:r['number'])
        calls = [json.loads(line) for line in self.f.gh_log.read_text().splitlines()] if self.f.gh_log.exists() else []
        bodies = [a[a.index('--body')+1] for a in calls if a[:2] == ['pr', 'comment']]
        return record, bodies[-1] if bodies else '', capture

    def test_three_calls_fixed_headers_and_round_count(self):
        record, body, _ = self.run_review(20)
        self.assertEqual(record['calls'], 3); self.assertEqual(record['usage']['input_tokens'], 3)
        for text in ('## Code Review v2', '**Verdict**: APPROVE', '**Reviewed head**:', '**Target**:', '## All Findings'):
            self.assertIn(text, body)
        self.assertIn('round=1 verdict=APPROVE', body)
        second, body, _ = self.run_review(20)
        self.assertEqual(second['number'], 2); self.assertIn('round=2 verdict=APPROVE', body)
        comments = json.loads(self.f.linear.read_text())['T-1']['comments']['nodes']
        helpers = re.search(r'^review_counts\(\) \{\n.*?^\}', (SCRIPTS / 'bureau-config.sh').read_text(), re.M | re.S).group(0)
        counts = subprocess.check_output(['/bin/bash', '-c', helpers + '\nreview_counts'], input=json.dumps(comments), text=True)
        self.assertEqual(json.loads(counts), dict(cycles=0, rounds=2))

    def test_malformed_specialist_fallback_is_recorded_metadata_only(self):
        self.values['performance'] = 'Malformed specialist text'
        record, body, _ = self.run_review(0)
        self.assertEqual(record['calls'], 4); self.assertEqual(record['usage']['input_tokens'], 4)
        self.assertEqual(record['merge']['mode'], 'model_fallback')
        self.assertEqual(record['merge']['malformed_specialists'], ['performance'])
        self.assertNotIn('Malformed specialist text', json.dumps(record)); self.assertIn('**Verdict**: REQUEST_CHANGES', body)

    def test_invalid_anchor_off_is_observational_required_blocks(self):
        self.values['correctness'] = specialist('correctness', [self.claim(invalid=True)])
        record, body, _ = self.run_review(20)
        self.assertEqual(record['merge']['invalid_anchors'], 1); self.assertIn('**Verdict**: APPROVE', body)
        self.config['agents']['review_grounding'] = 'required'
        record, body, _ = self.run_review(25)
        self.assertEqual(record['final_verdict'], 'BLOCK'); self.assertIn('**Source grounding**: BLOCK', body)

    def test_valid_anchor_required_never_weakens_critical(self):
        self.config['agents']['review_grounding'] = 'required'
        self.values['correctness'] = specialist('correctness', [self.claim('CRITICAL')])
        record, body, _ = self.run_review(25)
        self.assertEqual(record['merge']['invalid_anchors'], 0); self.assertIn('**Verdict**: BLOCK', body)

    def test_security_count_is_its_own_floor(self):
        self.values['security']['counts']['critical'] = 1
        record, body, _ = self.run_review(25)
        self.assertEqual(record['final_verdict'], 'BLOCK'); self.assertIn('**Verdict**: BLOCK', body)

    def test_shadow_refutation_does_not_change_approval_or_security_floor(self):
        self.config['agents']['runner'] = 'claude'
        cli = self.root / 'bin/claude'; cli.write_text('#!' + sys.executable + '\n' + PROVIDER); cli.chmod(0o755)
        self.config['agents']['finding_verifier'] = dict(enabled=True)
        self.values['correctness'] = specialist('correctness', [self.claim()])
        record, body, capture = self.run_review(20)
        self.assertEqual(record['calls'], 4); self.assertEqual(record['merge']['verifier'][0]['outcome'], 'refuted')
        self.assertIn('**Verdict**: APPROVE', body)
        prompt = (capture / 'finding_verifier.txt').read_text()
        self.assertNotIn('correctness summary', prompt); self.assertIn('Task implementation', prompt)
        self.values['security'] = specialist('security', [self.claim('CRITICAL')])
        record, body, _ = self.run_review(25)
        self.assertEqual(record['merge']['verifier'][0]['outcome'], 'refuted'); self.assertEqual(record['final_verdict'], 'BLOCK')

    def test_codex_shadow_without_tool_free_support_remains_unverifiable(self):
        self.config['agents']['finding_verifier'] = dict(enabled=True)
        self.values['correctness'] = specialist('correctness', [self.claim()])
        record, body, capture = self.run_review(20)
        self.assertEqual(record['merge']['verifier'][0]['outcome'], 'unverifiable')
        self.assertEqual(record['calls'], 4); self.assertIsNone(record['usage']['input_tokens'])
        self.assertFalse((capture / 'finding_verifier.txt').exists())
        self.assertIn('**Verdict**: APPROVE', body)

    def test_required_fallback_invalid_anchor_blocks_parse_error_is_22(self):
        self.config['agents']['review_grounding'] = 'required'
        self.values['performance'] = 'bad specialist'
        self.values['verdict']['findings'] = [self.claim(invalid=True)]
        record, body, _ = self.run_review(25)
        self.assertEqual(record['merge']['mode'], 'model_fallback'); self.assertIn('**Verdict**: BLOCK', body)
        self.values['verdict'] = '{bad json'
        record, body, _ = self.run_review(22)
        self.assertIsNone(record['final_verdict'])


if __name__ == '__main__': unittest.main()
