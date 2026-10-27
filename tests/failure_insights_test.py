"""Local learning records: real Git/store/stages; service and provider edges are fake."""
import concurrent.futures
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import review_convergence_test as review
import provider_adapter_test as provider_fixture
import spec_resume_test as spec_fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_TEST_INSIGHTS_SCRIPTS', ROOT / 'templates/scripts'))
spec = importlib.util.spec_from_file_location('failure_evidence', SCRIPTS / 'bureau-evidence.py')
e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e)


def append_failure(args):
    directory, receipt = args
    e.Store(directory).put_receipt(receipt)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-1753-store-'); self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'; self.repo.mkdir()
        for args in [('init', '-q', '-b', 'main'), ('config', 'maintenance.auto', 'false'),
                     ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.invalid'),
                     ('commit', '-qm', 'fixture', '--allow-empty')]: self.git(*args)
        self.head = self.git('rev-parse', 'HEAD'); self.now = time.time()
        self.store = e.Store(self.repo / '.git/bureau', clock=lambda: self.now)
        self.run = uuid.uuid4().hex
        self.store.begin_run(self.run, 'T-1', self.repo, self.head)
        self.attempt = self.start()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.STDOUT).strip()

    def start(self, stage='implement', phase='stage', parent=None):
        return self.store.begin_attempt(self.run, 'T-1', stage, phase, self.repo, self.head, parent=parent)

    def rows(self, name):
        path = self.store.root / name
        self.assertTrue(path.exists(), 'missing learning ledger: ' + name)
        return [json.loads(line) for line in path.read_text().splitlines()]

    def receipt(self, **changes):
        value = dict(run_id=self.run, stage_attempt_id=self.attempt, provider_attempt_id=uuid.uuid4().hex,
                     issue='T-1', stage='implement', phase='implement', provider='codex', normalized_exit=25,
                     provider_exit=0, outcome='complete', started_at=self.now, ended_at=self.now,
                     start_head=self.head, end_head=self.head, provider_logs=str(self.repo),
                     structured_result=dict(insights=['The bounded runner required a smaller work unit.'],
                                            cause_class='MODEL_CAUSE_CANARY', summary='FINDING_PROSE_CANARY'))
        value.update(changes); return value

    def finish(self, code=25, attempt=None):
        self.store.end_attempt(self.run, attempt or self.attempt, code, self.head)
        return self.rows('stage-outcomes.jsonl')[-1]

    def test_shell_cause_matrix_uses_only_metadata(self):
        shell = SCRIPTS / 'bureau-insights.sh'; self.assertTrue(shell.exists(), 'missing shell cause classifier')
        cases = [(124, '', '', '', 'timeout'), (25, 'timeout', 'silence', '', 'timeout'),
                 (14, '', '', '', 'gate_red'), (25, '', '', 'review_capped', 'review_capped'),
                 (21, '', '', '', 'ownership'), (11, '', '', '', 'ownership'),
                 (22, '', '', '', 'provider_error'), (16, '', '', '', 'provider_error'),
                 (25, 'invalid-result', '', '', 'provider_error'), (23, '', '', '', 'quota'),
                 (20, '', '', '', 'human_hold'), (25, '', '', '', 'human_hold'),
                 (24, '', '', '', 'other'), (27, '', '', '', 'other'),
                 (0, '', '', '', 'other'), (29, 'quota', 'silence', 'review_capped', 'other')]
        for code, outcome, timeout, signal, expected in cases:
            with self.subTest(code=code, outcome=outcome, signal=signal):
                p = subprocess.run(['/bin/bash', str(shell), str(code), outcome, timeout, signal], capture_output=True, text=True, check=True)
                self.assertEqual(p.stdout.strip(), expected)

    def test_failed_insights_for_every_stage_and_all_causes(self):
        for stage in ('spec', 'spec_review', 'ux', 'copy', 'implement', 'qa', 'code_review', 'merge', 'rebase'):
            for code, outcome, expected in [(124, 'timeout', 'timeout'), (14, 'complete', 'gate_red'),
                                            (21, 'complete', 'ownership'), (22, 'provider-error', 'provider_error'),
                                            (23, 'quota', 'quota'), (25, 'complete', 'human_hold'), (24, 'environment', 'other')]:
                r = self.receipt(stage=stage, normalized_exit=code, outcome=outcome)
                self.store.put_receipt(r)
                row = self.rows('insights.jsonl')[-1]
                self.assertEqual((row['stage'], row.get('normalized_exit'), row.get('cause_class')), (stage, code, expected))
                self.assertNotIn('MODEL_CAUSE_CANARY', json.dumps(row))

    def test_no_result_still_records_each_stage_outcome_once(self):
        stages = ('spec', 'spec_review', 'ux', 'copy', 'implement', 'qa', 'code_review', 'merge', 'rebase')
        for stage in stages:
            attempt = self.start(stage)
            row = self.finish(22, attempt); self.finish(22, attempt)
            self.assertEqual((row['stage'], row['cause_class'], row['normalized_exit']), (stage, 'provider_error', 22))
            self.assertNotIn('observation', row)
        self.assertEqual(len(self.rows('stage-outcomes.jsonl')), len(stages))

    def test_silence_is_timeout_without_result_and_prose_cannot_claim_it(self):
        self.store.put_receipt(self.receipt(normalized_exit=25, outcome='timeout', timeout_kind='silence', structured_result=None))
        self.assertEqual(self.finish()['cause_class'], 'timeout')
        self.attempt = self.start()
        self.store.put_receipt(self.receipt(structured_result=dict(timeout_kind='silence', cause_class='timeout')))
        self.assertEqual(self.finish()['cause_class'], 'human_hold')
        self.attempt = self.start()
        self.store.put_receipt(self.receipt(normalized_exit=22, outcome='provider-error', structured_result=None))
        self.store.put_receipt(self.receipt(normalized_exit=0))
        self.assertEqual(self.finish()['cause_class'], 'human_hold', 'recovered provider error cannot explain a later hold')

    def test_trusted_review_cap_and_red_gate_signals(self):
        self.attempt = self.start('code_review')
        self.assertTrue(hasattr(self.store, 'outcome_signal'), 'missing trusted outcome signal')
        self.store.outcome_signal(self.run, self.attempt, 'review_capped')
        self.assertEqual(self.finish()['cause_class'], 'review_capped')
        self.attempt = self.start('qa')
        self.store.outcome_signal(self.run, self.attempt, 'gate_red')
        row = self.finish(0); self.assertEqual((row['normalized_exit'], row['cause_class']), (0, 'gate_red'))
        self.attempt = self.start('qa')
        self.store.outcome_signal(self.run, self.attempt, 'human_hold')
        log = self.repo / 'gate'; log.write_text('PRIVATE_GATE_CANARY')
        self.store.gate(self.run, self.attempt, log, self.head, self.head, 'configured check', 1)
        self.store.gate(self.run, self.attempt, log, self.head, self.head, 'configured check', 0)
        self.assertEqual(self.finish(25)['cause_class'], 'human_hold', 'an earlier red gate cannot explain a later human hold')
        self.attempt = self.start('qa')
        self.store.outcome_signal(self.run, self.attempt, 'human_hold')
        self.assertEqual(self.finish(0)['cause_class'], 'human_hold')

    def test_withdrawal_and_quiet_wait_override_prior_failures(self):
        self.store.put_receipt(self.receipt(normalized_exit=25, outcome='timeout', timeout_kind='silence'))
        control = self.repo / '.git/bureau/ticket-control' / self.run; control.mkdir(parents=True)
        for code, expected in [(0, 'other'), (25, 'human_hold'), (26, 'other'), (29, 'other'), (130, 'other')]:
            self.attempt = self.start(); (control / ('stop-' + str(code))).touch()
            self.store.put_receipt(self.receipt(outcome='quota', normalized_exit=23))
            row = self.finish(code)
            self.assertEqual((row['normalized_exit'], row['cause_class']), (code, expected))
            (control / ('stop-' + str(code))).unlink()

    def test_dispositions_project_severity_path_and_no_finding_prose(self):
        items = [dict(disposition='applied', severity='BUG', path='src/check.py'),
                 dict(disposition='declined', severity='MINOR', path='src/name.py')]
        r = self.receipt(normalized_exit=0, structured_result=dict(review_dispositions=items, prose_notes='FINDING_PROSE_CANARY'))
        self.store.put_receipt(r); self.store.put_receipt(r)
        rows = self.rows('review-dispositions.jsonl'); self.assertEqual(len(rows), 2)
        self.assertEqual([{k: row[k] for k in ('disposition', 'severity', 'path')} for row in rows], items)
        for row in rows:
            self.assertEqual(set(row), {'schema_version', 'record_type', 'issue', 'stage', 'run_id', 'stage_attempt_id',
                                        'provider_attempt_id', 'timestamp', 'disposition', 'severity', 'path'})
        excessive = self.receipt(structured_result=dict(review_dispositions=[items[0]] * 33))
        self.store.put_receipt(excessive)
        self.assertEqual(len(self.rows('review-dispositions.jsonl')), 2)
        review_receipt = self.receipt(stage='code_review', phase='verdict', normalized_exit=0, structured_result=dict(
            findings=[dict(file=f'src/pin{i}.py', **{'class':'SKIP'}, msg='FINDING_PROSE_CANARY') for i in range(40)]))
        self.store.put_receipt(review_receipt)
        self.assertEqual(len(self.rows('review-dispositions.jsonl')), 34, 'review projection must cap at 32 rows per attempt')
        self.assertNotIn('FINDING_PROSE_CANARY', b''.join(p.read_bytes() for p in self.store.root.rglob('*') if p.is_file()).decode())

    def test_skip_and_deferred_follow_floor_without_prose(self):
        for floor, expected in [('BUG', ['deferred', 'skip']), ('MINOR', ['skip'])]:
            r = self.receipt(stage='code_review', phase='verdict', normalized_exit=0, review_min_severity=floor,
                             structured_result=dict(findings=[dict(file='src/minor.py', **{'class':'MINOR'}, msg='FINDING_PROSE_CANARY'),
                                                              dict(file='src/pin.py', **{'class':'SKIP'}, msg='PIN_PROSE_CANARY')]))
            self.store.put_receipt(r)
            rows = [row for row in self.rows('review-dispositions.jsonl') if row['provider_attempt_id'] == r['provider_attempt_id']]
            self.assertEqual([row['disposition'] for row in rows], expected)
            self.assertNotIn('PROSE_CANARY', json.dumps(rows))

    def test_privacy_and_malformed_dispositions_preserve_failure_insight(self):
        for path in ('/Users/private/client.py', '../private', 'src/../../private', 'src/TOKEN=canary',
                     'src/' + 'a'*40, 'src/INFO.log', 'src/private\ntext', 'src/private\x00file'):
            r = self.receipt(structured_result=dict(insights=['The runner needed a smaller work unit.'],
                review_dispositions=[dict(disposition='applied', severity='BUG', path=path)]))
            self.store.put_receipt(r)
            self.assertEqual(self.rows('insights.jsonl')[-1].get('cause_class'), 'human_hold')
            self.assertFalse((self.store.root / 'review-dispositions.jsonl').exists())
        for payload in (None, ['FINDING_PROSE_CANARY'], [dict(disposition='applied', severity='BUG', path='src/a', reason='FINDING_PROSE_CANARY')]):
            r = self.receipt(structured_result=dict(review_dispositions=payload))
            self.store.put_receipt(r)
            self.assertFalse((self.store.root / 'review-dispositions.jsonl').exists())

    def test_failed_privacy_filter_drops_canaries_before_store_write(self):
        safe = 'A lower concurrency setting avoided a local resource limit.'
        for private in ('TOKEN=CANARY_SECRET', 'ghp_CANARY_SECRET', '/Users/canary/private', 'INFO CANARY_LOG',
                        '$ cat CANARY_LOG', 'a'*40, 'CANARY\nPRIVATE'):
            r = self.receipt(structured_result=dict(insights=[private, safe] if '\n' not in private else [private]))
            self.store.put_receipt(r)
        rows = self.rows('insights.jsonl')
        self.assertTrue(all(row.get('cause_class') == 'human_hold' for row in rows))
        data = b''.join(p.read_bytes() for p in self.store.root.rglob('*') if p.is_file()).decode()
        for private in ('CANARY_SECRET', 'CANARY_LOG', '/Users/canary', 'CANARY\\nPRIVATE'): self.assertNotIn(private, data)

    def test_shared_lock_and_optional_failure_cannot_replace_receipt(self):
        r = self.receipt(); self.store.put_receipt(r)
        self.assertTrue(hasattr(self.store, '_append_learning'), 'missing shared learning writer')
        with patch.object(self.store, '_append_learning', side_effect=OSError('PRIVATE_FAILURE')):
            path = self.store.put_receipt(self.receipt(normalized_exit=0))
            self.assertEqual(json.loads(path.read_text())['normalized_exit'], 0)
        with (self.store.root / 'guard').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = time.monotonic()
            with self.assertRaises(TimeoutError): self.store._append_learning('insights.jsonl', [self.rows('insights.jsonl')[0]], ('provider_attempt_id',), self.run)
            self.assertLess(time.monotonic() - started, 1)

    def test_retention_caps_permissions_and_capacity_for_all_ledgers(self):
        self.store.put_receipt(self.receipt(structured_result=dict(insights=['The runner needed smaller work.'],
            review_dispositions=[dict(disposition='applied', severity='BUG', path='src/a.py')])))
        self.finish()
        for name in ('insights.jsonl', 'stage-outcomes.jsonl', 'review-dispositions.jsonl'):
            rows = self.rows(name); seed = rows[0]
            self.now += e.RETENTION_SECONDS + 1
            incoming = [{**seed, 'provider_attempt_id':f'{i:032x}', 'stage_attempt_id':f'{i:032x}', 'timestamp':self.now} for i in range(2050)]
            self.store._append_learning(name, incoming, ('provider_attempt_id',), self.run)
            rows = self.rows(name); self.assertEqual(len(rows), 2048)
            self.assertEqual(rows[0]['provider_attempt_id'], f'{2:032x}')
            self.assertTrue(all(row['timestamp'] == self.now for row in rows))
            path = self.store.root / name; self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.read_bytes()
            with patch.object(e, 'MAX_BYTES', 1), self.assertRaises(e.CapacityError):
                self.store._append_learning(name, [{**seed, 'provider_attempt_id':'f'*32}], ('provider_attempt_id',), self.run)
            self.assertEqual(path.read_bytes(), before)

    def test_concurrent_failed_collection_and_linked_worktree_dedup(self):
        receipts = [self.receipt() for _ in range(8)]
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
            list(pool.map(append_failure, [(self.repo / '.git/bureau', r) for r in receipts] * 2))
        linked = self.repo.parent / 'linked'; self.git('worktree', 'add', '-q', '-b', 'linked', str(linked))
        common = subprocess.check_output(['git', '-C', str(linked), 'rev-parse', '--git-common-dir'], text=True).strip()
        other = e.Store((linked / common).resolve() / 'bureau'); other.put_receipt(receipts[0])
        self.assertEqual(len(self.rows('insights.jsonl')), 8)
        self.assertTrue(all(row.get('cause_class') == 'human_hold' for row in self.rows('insights.jsonl')))

    def test_worker_outcome_does_not_duplicate_nested_stage(self):
        worker = self.start('qa', 'worker'); stage = self.start('qa', parent=worker)
        self.finish(14, stage); self.finish(14, worker)
        rows = self.rows('stage-outcomes.jsonl'); self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stage_attempt_id'], stage)

    def test_inline_merge_keeps_parent_review_outcome_and_observation_join(self):
        worker = self.start('code_review', 'worker')
        parent = self.start('code_review', parent=worker)
        child = self.start('merge', parent=parent)
        self.store.put_receipt(self.receipt(stage='code_review', phase='verdict',
                                           stage_attempt_id=parent, normalized_exit=0))
        self.finish(25, child); self.finish(25, parent); self.finish(25, worker)
        self.finish(25, parent); self.finish(25, worker)
        rows = self.rows('stage-outcomes.jsonl')
        self.assertEqual({r['stage_attempt_id'] for r in rows}, {parent, child},
                         'inline merge must keep the parent stage and suppress only its worker')
        self.assertEqual(len(rows), 2)
        self.assertEqual({(r['stage'], r['normalized_exit'], r['cause_class']) for r in rows},
                         {('code_review', 25, 'human_hold'), ('merge', 25, 'human_hold')})
        outcomes = {r['stage_attempt_id']:r for r in rows}
        for observation in self.rows('insights.jsonl'):
            self.assertEqual(outcomes[observation['stage_attempt_id']]['stage'], observation['stage'])

    def test_comment_projection_strips_nested_local_dispositions(self):
        import re
        config = (SCRIPTS / 'bureau-config.sh').read_text()
        project = re.search(r'^bureau_comment_projection\(\) \{\n.*?^\}', config, re.M | re.S)[0]
        raw = json.dumps(dict(status='NEEDS_HUMAN', review_dispositions=[dict(path='PRIVATE_DISPOSITION_PATH')],
                              result=json.dumps(dict(review_dispositions=[dict(path='PRIVATE_NESTED_PATH')]))))
        for text in (raw, json.dumps(dict(result=raw)), '```json\n'+raw+'\n```',
                     '```json\n{"review_dispositions": PRIVATE_MALFORMED\n```',
                     'review_dispositions: PRIVATE_ASSIGNMENT'):
            p = subprocess.run(['/bin/bash', '-c', project + '\nbureau_comment_projection "$1"', '_', text], capture_output=True, text=True, check=True)
            self.assertNotIn('PRIVATE_', p.stdout)
            if 'NEEDS_HUMAN' in text: self.assertIn('NEEDS_HUMAN', p.stdout)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.review = review.ReviewRoundPipelineTests('test_minor_only_request_changes_converges')
        with patch.object(review, 'SCRIPTS', SCRIPTS), patch.object(review, 'REVIEW', SCRIPTS / 'code-review-pipeline.sh'), patch.object(review, 'CONFIG', SCRIPTS / 'bureau-config.sh'):
            self.review.setUp()
        self.addCleanup(self.review.doCleanups)
        self.repo = self.review.repo; self.root = self.review.root
        curl = self.root / 'bin/curl'
        source = curl.read_text()
        source = source.replace("    if 'issueAddLabel' in query:", "    if 'issueRemoveLabel' in query:\n        items[key]['labels']['nodes'] = [r for r in items[key]['labels']['nodes'] if r['name'] != variables['lid']]; result = {'issueRemoveLabel': {'success': True}}\n    elif 'issueAddLabel' in query:")
        curl.write_text(source)

    def rows(self, name):
        path = self.repo / '.git/bureau/evidence/v1' / name
        self.assertTrue(path.exists(), 'missing pipeline learning ledger: ' + name)
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_real_review_floor_records_skip_deferred_and_keeps_round(self):
        value = review.result(findings=[review.finding('MINOR', 'FINDING_PROSE_CANARY'), review.finding('SKIP', 'PIN_PROSE_CANARY')])
        self.review.review(value)
        rows = self.rows('review-dispositions.jsonl')
        self.assertEqual({r['disposition'] for r in rows}, {'skip', 'deferred'})
        self.assertEqual(len(self.review.rounds()), 1); self.assertEqual(self.review.rounds()[0]['calls'], 4)
        self.assertNotIn('PROSE_CANARY', json.dumps(rows))

    def test_real_cap_records_review_capped_and_preserves_exit_25(self):
        self.review.config['agents']['max_review_rounds'] = 1
        self.review.review(review.result(findings=[review.finding('BUG')]), code=25)
        row = self.rows('stage-outcomes.jsonl')[-1]
        self.assertEqual((row['stage'], row['normalized_exit'], row['cause_class']), ('code_review', 25, 'review_capped'))
        self.assertEqual(self.review.rounds()[0]['final_verdict'], 'BLOCK')

    def test_real_provider_failure_has_cause_without_observations(self):
        self.review.review(review.result(), code=22, FAIL_MERGER='1')
        row = self.rows('stage-outcomes.jsonl')[-1]
        self.assertEqual((row['normalized_exit'], row['cause_class']), (22, 'provider_error'))
        self.assertIsNone(self.review.rounds()[0]['final_verdict'])

    def test_real_qa_human_hold_zero_keeps_routing_and_local_cause(self):
        completed = subprocess.run(['/bin/bash', 'scripts/bureau-worker.sh', 'T-3', 'qa-pipeline.sh',
                                    str(self.root/'qa-worker'), '003-task'], cwd=self.repo,
                                   env={**self.review.fixture.env, 'ROUND_USAGE':json.dumps(self.review.usage)}, capture_output=True, text=True, timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        row = self.rows('stage-outcomes.jsonl')[-1]
        self.assertEqual((row['stage'], row['normalized_exit'], row['cause_class']), ('qa', 0, 'human_hold'))

    def test_zero_exit_implement_ux_holds_and_approved_red_review_keep_causes(self):
        codex = self.root / 'bin/codex'
        codex.write_text(codex.read_text().replace(
            "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))",
            """if os.environ.get('FAILURE_TEST_STAGE') == 'implement':
    result = dict(status='NEEDS_HUMAN', tasks_done=0, tasks_skipped=0, tasks_needs_human=1,
                  fixed_review_items=[], notes=dict(needs_human=[dict(task_id='T001', reason='A product decision is required')],
                  skipped=[], deviations=[]), prose_notes='A product decision is required')
elif os.environ.get('FAILURE_TEST_STAGE') == 'ux':
    result = dict(design_status='NEEDS_HUMAN', summary='A design decision is required')
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))"""))
        for stage, issue, state in [('implement', 'T-2', 'Build'), ('ux', 'T-3', 'Design')]:
            with self.subTest(stage=stage):
                items = json.loads(self.review.fixture.linear.read_text())
                items[issue]['state'] = dict(id=state.lower(), name=state)
                self.review.fixture.linear.write_text(json.dumps(items))
                completed = subprocess.run(['/bin/bash', 'scripts/bureau-worker.sh', issue, stage+'-pipeline.sh',
                                            str(self.root/(stage+'-worker')), '00'+issue[-1]+'-task'], cwd=self.repo,
                                           env={**self.review.fixture.env, 'FAILURE_TEST_STAGE':stage,
                                                'ROUND_USAGE':json.dumps(self.review.usage)},
                                           capture_output=True, text=True, timeout=45)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertIn('needs-human', [r['name'] for r in json.loads(
                    self.review.fixture.linear.read_text())[issue]['labels']['nodes']])
                row = self.rows('stage-outcomes.jsonl')[-1]
                self.assertEqual((row['stage'], row['normalized_exit'], row['cause_class']),
                                 (stage, 0, 'human_hold'))
        with self.subTest(stage='code_review'):
            self.review.config['repo']['test_command'] = 'false'
            self.review.review(review.result('APPROVE', findings=[]), code=0)
            self.assertEqual(self.review.rounds()[0]['final_verdict'], 'REQUEST_CHANGES')
            row = self.rows('stage-outcomes.jsonl')[-1]
            self.assertEqual((row['stage'], row['normalized_exit'], row['cause_class']),
                             ('code_review', 0, 'gate_red'))

    def test_shepherd_emits_exact_worker_outcome_before_checkpoint(self):
        config = self.review.config
        config['agents']['merge_mode'] = 'manual'; (self.repo / '.bureau.json').write_text(json.dumps(config))
        # The real driver, worker, runtime and review stage run; providers and Linear use fixture transports.
        p = subprocess.Popen(['/bin/bash', 'scripts/shepherd.sh', '--no-tmux', '--no-merge', '--worktree', str(self.root/'shepherd-worker'), 'T-1'],
                           cwd=self.repo, env={**self.review.fixture.env, 'REVIEW_RESULT_JSON':json.dumps(review.result('BLOCK', findings=[])),
                                               'ROUND_USAGE':json.dumps(self.review.usage), 'BUREAU_DISPATCH_LANE':'1'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = p.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            stdout, stderr = p.communicate(timeout=10)
            self.fail('shepherd timed out: ' + stdout + stderr)
        finally:
            if p.poll() is None: os.killpg(p.pid, signal.SIGKILL); p.wait()
        self.assertEqual(p.returncode, 25, stdout + stderr)
        events = [json.loads(line) for line in (self.repo/'logs/events.jsonl').read_text().splitlines()]
        ends = [r for r in events if r['event'] == 'stage_end' and r.get('mode') == 'shepherd']
        self.assertEqual(len(ends), 1, 'missing or duplicated shepherd outcome')
        row = ends[0]; self.assertEqual((row['stage'], row['exit_code'], row['class']), ('code-review-pipeline.sh', 25, 'needs-human-or-paused'))
        self.assertIsInstance(row['duration_s'], int); self.assertGreaterEqual(row['duration_s'], 0)


class SpecPipelineTests(unittest.TestCase):
    def test_ignored_research_quota_does_not_explain_later_branch_divergence(self):
        fixture = spec_fixture.SpecWorkerTests('test_diverged_feature_halts_once_needs_human_before_provider')
        with patch.object(spec_fixture, 'SCRIPTS', SCRIPTS): fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.worker()
        fixture.advance_remote_feature()
        fixture.git('switch', '-q', '001-team-1', repo=fixture.wt)
        (fixture.wt/'specs/001-team-1/local-note.md').write_text('Local completed work\n')
        fixture.git('add', '.', repo=fixture.wt); fixture.git('commit', '-qm', 'local-only note', repo=fixture.wt)
        fixture.git('checkout', '-q', '--detach', repo=fixture.wt)
        fixture.write('state', 'Triage'); fixture.write('calls', '')
        config = json.loads((fixture.repo/'.bureau.json').read_text())
        config['agents'].update(runner='codex', research=True); fixture.write_config(config)
        detail = json.loads((fixture.root/'detail.json').read_text())
        detail['labels'] = ['needs-research']; fixture.write('detail.json', json.dumps(detail))
        # Keep the real research adapter/receipt path; only the provider transport fails.
        binary = fixture.root/'bin'; binary.mkdir()
        codex = binary/'codex'
        codex.write_text('#!'+sys.executable+"\nimport sys\nif sys.argv[1] == 'login': sys.exit(0)\n"
                         "print('quota exceeded', file=sys.stderr)\nsys.exit(1)\n")
        codex.chmod(0o755)
        fixture.env['PATH'] = str(binary)+os.pathsep+fixture.env['PATH']
        adapter = review.function(SCRIPTS/'bureau-config.sh', 'run_stage_for').replace(
            'run_stage_for() {', 'provider_stage_for() {', 1)
        with (fixture.repo/'scripts/bureau-config.sh').open('a') as out:
            out.write('\n_fixture_stage=$(declare -f run_stage_for)\n'
                      'eval "${_fixture_stage/run_stage_for/fixture_stage_for}"\n'+adapter+'''
run_stage_for() {
  if [ "$1" = research ]; then provider_stage_for "$@"; else fixture_stage_for "$@"; fi
}
''')
        completed = fixture.worker(expect=25)
        self.assertIn('must reconcile both histories', completed.stderr)
        self.assertEqual(fixture.calls(), [], 'ignored research must continue to the branch decision before any spec phase')
        root = fixture.repo/'.git/bureau/evidence/v1'
        receipts = [json.loads(p.read_text()) for p in root.glob('runs/*/receipts/*.json')]
        research = [r for r in receipts if r['stage'] == 'research']
        self.assertEqual(len(research), 1, completed.stdout + completed.stderr)
        self.assertEqual((research[0]['normalized_exit'], research[0]['outcome']), (23, 'quota'))
        rows = [json.loads(line) for line in (root/'stage-outcomes.jsonl').read_text().splitlines()]
        row = next(r for r in rows if r['stage_attempt_id'] == research[0]['stage_attempt_id'])
        self.assertEqual((row['stage'], row['normalized_exit'], row['cause_class']), ('spec', 25, 'human_hold'))


class ProviderRecordTests(unittest.TestCase):
    def test_real_failed_provider_captures_only_filtered_advisory_text(self):
        fixture = provider_fixture.ProviderTests('test_insights_content_filter_preserves_provider_success')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fake = fixture.fake.replace("if os.environ.get('FAIL'):\n", """if os.environ.get('FAIL'):
    if name == 'codex': pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(os.environ['FAILED_FINAL'])
    else: print(json.dumps({'result':os.environ['FAILED_FINAL']}))
""")
        for name in ('codex', 'claude'): (fixture.bin/name).write_text(fake)
        safe = 'A smaller provider work unit avoided the transient backend limit.'
        value = dict(insights=[safe, 'TOKEN=PRIVATE_CANARY'], summary='FINDING_PROSE_CANARY', cause_class='review_capped')
        with patch.object(provider_fixture, 'SCRIPT', SCRIPTS / 'bureau-provider.py'):
            for runner in ('codex', 'claude'):
                fixture.config.write_text(json.dumps(dict(agents=dict(runner=runner))))
                completed = fixture.run_provider(FAIL='backend failed', FAILED_FINAL=json.dumps(value), code=22)
                self.assertEqual(completed.stdout, '')
        path = fixture.root / '.git/bureau/evidence/v1/insights.jsonl'
        self.assertTrue(path.exists(), 'missing failed provider observation')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all((r['observation'], r['normalized_exit'], r['cause_class']) == (safe, 22, 'provider_error') for r in rows))
        data = b''.join(p.read_bytes() for p in path.parent.rglob('*') if p.is_file()).decode()
        self.assertNotIn('PRIVATE_CANARY', data); self.assertNotIn('FINDING_PROSE_CANARY', data)
        for path in path.parent.glob('runs/*/receipts/*.json'):
            receipt = json.loads(path.read_text()); self.assertIsNone(receipt['structured_result']); self.assertIsNone(receipt['result_sha256'])

    def test_real_provider_disposition_contract_and_metadata_projection(self):
        fixture = provider_fixture.ProviderTests('test_insights_content_filter_preserves_provider_success')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        items = [dict(disposition='applied', severity='BUG', path='src/check.py'),
                 dict(disposition='declined', severity='MINOR', path='src/name.py')]
        value = {**fixture.complete_implement_result(), 'review_dispositions':items, 'insights':[]}
        with patch.object(provider_fixture, 'SCRIPT', SCRIPTS / 'bureau-provider.py'):
            for runner in ('codex', 'claude'):
                fixture.config.write_text(json.dumps(dict(agents=dict(runner=runner))))
                completed = fixture.run_provider('--schema', str(SCRIPTS / 'bureau-implement.schema.json'),
                                                  FINAL=json.dumps(value), ASSERT_STRICT_SCHEMA='1')
                self.assertEqual(json.loads(completed.stdout)['review_dispositions'], items)
        path = fixture.root / '.git/bureau/evidence/v1/review-dispositions.jsonl'
        self.assertTrue(path.exists(), 'missing provider disposition ledger')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 4); self.assertEqual({r['disposition'] for r in rows}, {'applied', 'declined'})
        for path in (fixture.root / '.git/bureau/evidence/v1/runs').glob('*/receipts/*.json'):
            receipt = json.loads(path.read_text()); self.assertIsNone(receipt['structured_result'])
            self.assertNotIn('review_dispositions', receipt)


if __name__ == '__main__': unittest.main()
