"""Convergence rules and real review rounds; provider/Linear/GitHub calls are fake."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import supervision_pipeline_test as fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
REVIEW = Path(os.environ.get('BUREAU_TEST_REVIEW_SCRIPT', SCRIPTS / 'code-review-pipeline.sh'))
CONFIG = Path(os.environ.get('BUREAU_TEST_REVIEW_CONFIG', SCRIPTS / 'bureau-config.sh'))


def function(path, name):
    return re.search(r'^' + name + r'\(\) \{\n.*?^\}', path.read_text(), re.M | re.S)[0]


def finding(severity, msg='A concrete finding'):
    return dict(file='src/check.py', line=12, **{'class': severity}, msg=msg)


def result(verdict='REQUEST_CHANGES', findings=None, **changes):
    findings = [finding('MINOR')] if findings is None else findings
    return {**dict(verdict=verdict, bugs=sum(f['class'] == 'BUG' for f in findings), security_issues=0,
                   missing_acceptance=[], fixes_needed=[], summary='Review complete', comment='Public review.', findings=findings),
            **changes}


def shell(source, raw='', **env):
    helpers = '\n'.join(function(CONFIG, name) for name in (
        'parse_claude_json', 'review_convergence', 'review_counts', 'apply_build_failure',
        'decide_review_verdict', '_review_count', '_review_shown', 'bureau_comment_projection'))
    completed = subprocess.run(['/bin/bash', '-euo', 'pipefail', '-c', helpers + '\n' + source],
                               input=raw, env={**os.environ, **env}, capture_output=True, text=True, timeout=10)
    if completed.returncode: raise AssertionError(completed.stderr)
    return completed.stdout.strip()


class DecisionTests(unittest.TestCase):
    def converge(self, value, floor='BUG'):
        return shell('raw=$(cat); review_convergence "$raw" "$FLOOR"', json.dumps(value), FLOOR=floor)

    def test_validated_findings_matrix(self):
        rows = [
            (result(), 'BUG', 'APPROVE'),
            (result(findings=[finding('SKIP')]), 'BUG', 'APPROVE'),
            (result(), 'MINOR', 'REQUEST_CHANGES'),
            (result('APPROVE'), 'MINOR', 'REQUEST_CHANGES'),
            (result(findings=[finding('BUG')]), 'BUG', 'REQUEST_CHANGES'),
            (result('APPROVE', [finding('BUG')]), 'BUG', 'REQUEST_CHANGES'),
            (result(findings=[finding('CRITICAL')]), 'BUG', 'BLOCK'),
            (result('BLOCK'), 'BUG', 'BLOCK'),
            (result(findings=[]), 'BUG', 'REQUEST_CHANGES'),
            (result(findings=[], bugs=1), 'BUG', 'BLOCK'),
            (result('APPROVE', findings=[], fixes_needed=['Unresolved work']), 'BUG', 'BLOCK'),
            (result(bugs=1), 'BUG', 'BLOCK'),
            (result(findings=[finding('BUG')], bugs=0), 'BUG', 'BLOCK'),
            (result(security_issues=1), 'BUG', 'REQUEST_CHANGES'),
            (result('APPROVE', missing_acceptance=['Required behavior absent']), 'BUG', 'REQUEST_CHANGES'),
            (result(missing_acceptance=['Required behavior absent']), 'BUG', 'REQUEST_CHANGES'),
            (result(security_issues=-1), 'BUG', 'BLOCK'),
            (result(bugs=True), 'BUG', 'BLOCK'),
            (result(findings=[{**finding('MINOR'), 'line': -1}]), 'BUG', 'BLOCK'),
            (result(findings=[{**finding('MINOR'), 'class': 'UNKNOWN'}]), 'BUG', 'BLOCK'),
            (result(), 'invalid', 'BLOCK'),
        ]
        for value, floor, expected in rows:
            with self.subTest(value=value, floor=floor): self.assertEqual(self.converge(value, floor), expected)
        for field in ('verdict', 'bugs', 'security_issues', 'missing_acceptance', 'findings', 'comment', 'fixes_needed'):
            value = result(); value.pop(field)
            with self.subTest(missing=field): self.assertEqual(self.converge(value), 'BLOCK')

    def test_every_required_fix_must_match_a_deferred_finding(self):
        minor = finding('MINOR', 'Rename the local variable')
        skipped = finding('SKIP', 'Keep the pinned parser (SPEC.md section 2)')
        for fixes in (['CRITICAL: save the replacement before deleting the original'],
                      [minor['msg'], 'Restore missing error handling'],
                      ['Rename the local variable and prevent data loss'], [''], ['   ']):
            with self.subTest(fixes=fixes):
                self.assertEqual(self.converge(result(findings=[minor, skipped], fixes_needed=fixes)),
                                 'REQUEST_CHANGES')

    def test_exact_required_fixes_can_be_deferred_but_uncertain_matches_cannot(self):
        minor = finding('MINOR', 'Rename the local variable')
        skipped = finding('SKIP', 'Keep the pinned parser (SPEC.md section 2)')
        value = result(findings=[minor, skipped], fixes_needed=[minor['msg'], skipped['msg']])
        self.assertEqual(self.converge(value), 'APPROVE')
        self.assertEqual(self.converge(value, 'MINOR'), 'REQUEST_CHANGES')
        self.assertEqual(self.converge(result(findings=[skipped], fixes_needed=[skipped['msg']]), 'MINOR'), 'APPROVE')
        self.assertEqual(self.converge(result(findings=[minor], fixes_needed=['rename the local variable'])),
                         'REQUEST_CHANGES')
        self.assertEqual(self.converge(result('APPROVE', [minor], fixes_needed=['Unexplained required fix'])),
                         'REQUEST_CHANGES')

    def test_configuration_defaults_derived_limit_and_invalid_floor(self):
        assignments = '\n'.join(line for line in CONFIG.read_text().splitlines() if re.match(
            r'^BUREAU_(MAX_REVIEW_CYCLES|MAX_REVIEW_ROUNDS|REVIEW_MIN_SEVERITY)=', line))
        source = function(SCRIPTS / 'bureau-env.sh', 'bureau_number') + '\n' + function(CONFIG, '_bureau_config_number')
        source += '\nbureau_get() { jq -r "$1" "$CONFIG_FILE"; }\n' + assignments
        source += '\nprintf "%s:%s:%s" "$BUREAU_MAX_REVIEW_CYCLES" "$BUREAU_MAX_REVIEW_ROUNDS" "$BUREAU_REVIEW_MIN_SEVERITY"'
        rows = [({}, '3:4:BUG'), ({'max_review_cycles':0}, '0:1:BUG'), ({'max_review_cycles':2}, '2:3:BUG'),
                ({'max_review_cycles':2, 'max_review_rounds':2}, '2:2:BUG'),
                ({'max_review_cycles':2, 'max_review_rounds':0}, '2:3:BUG'),
                ({'review_min_severity':'MINOR'}, '3:4:MINOR'), ({'review_min_severity':False}, '3:4:MINOR'),
                ({'review_min_severity':'unknown'}, '3:4:MINOR')]
        with tempfile.TemporaryDirectory(prefix='bureau-pr8-config-') as scratch:
            path = Path(scratch) / 'config.json'
            for agents, expected in rows:
                path.write_text(json.dumps(dict(agents=agents)))
                with self.subTest(agents=agents): self.assertEqual(shell(source, CONFIG_FILE=str(path)), expected)

    def test_deferred_and_skip_lists_use_the_insight_projection(self):
        value = result(findings=[finding('MINOR', 'insights = Private deferred canary.'), finding('SKIP', 'Pinned decision')])
        output = shell(function(REVIEW, 'review_text_from_merger') + '\nraw=$(cat); review_text_from_merger "$raw"', json.dumps(value))
        self.assertNotIn('Private deferred canary.', output)
        self.assertIn('Deferred below threshold (BUG)', output); self.assertIn('SKIP findings', output)
        self.assertIn('Pinned decision', output)
        output = shell(function(REVIEW, 'review_text_from_merger') + '\nraw=$(cat); review_text_from_merger "$raw"',
                       json.dumps(value), BUREAU_REVIEW_MIN_SEVERITY='MINOR')
        self.assertNotIn('Deferred below threshold', output); self.assertIn('SKIP findings', output)

    def test_build_security_and_caps_follow_convergence(self):
        rows = [
            (result(), 'false', '0', 0, 3, 1, 4, 'REQUEST_CHANGES'),
            (result('BLOCK'), 'false', '0', 0, 3, 1, 4, 'BLOCK'),
            (result(), 'true', '1', 0, 3, 1, 4, 'BLOCK'),
            (result(security_issues=1), 'true', '0', 0, 3, 1, 4, 'REQUEST_CHANGES'),
            (result(findings=[finding('BUG')]), 'true', '0', 0, 3, 2, 3, 'REQUEST_CHANGES'),
            (result(findings=[finding('BUG')]), 'true', '0', 0, 3, 3, 3, 'BLOCK'),
            (result(), 'true', '0', 0, 3, 3, 3, 'APPROVE'),
            (result(), 'false', '0', 0, 3, 3, 3, 'BLOCK'),
            (result(findings=[finding('BUG')]), 'true', '0', 3, 3, 4, 20, 'BLOCK'),
            (result(findings=[finding('BUG')]), 'true', '0', 2, 3, 3, 4, 'REQUEST_CHANGES'),
            (result(findings=[finding('BUG')]), 'true', '0', 3, 3, 4, 4, 'BLOCK'),
        ]
        for value, build, critical, cycles, cap, number, rounds, expected in rows:
            with self.subTest(build=build, cycles=cycles, number=number, rounds=rounds):
                output = shell('raw=$(cat); v=$(review_convergence "$raw" BUG); '
                               'decide_review_verdict "$v" "$SEC" "$CRIT" "$BUILD" "$CYCLES" "$CAP" "$ROUND" "$ROUNDS"',
                               json.dumps(value), SEC=str(value['security_issues']), CRIT=critical, BUILD=build,
                               CYCLES=str(cycles), CAP=str(cap), ROUND=str(number), ROUNDS=str(rounds))
                self.assertEqual(output.splitlines()[0], expected)


class CountsTests(unittest.TestCase):
    def counts(self, bodies):
        return json.loads(shell('review_counts', json.dumps([dict(body=b) for b in bodies])))

    def test_legacy_header_counts_a_completed_round(self):
        # Read the actual emitted header, rather than testing a duplicate literal.
        header = re.search(r'post_comment "\$ISSUE" "(🔄 [^\n]+)', REVIEW.read_text())[1]
        self.assertEqual(self.counts([header]), dict(cycles=1, rounds=1))

    def test_structured_rounds_take_priority_and_reuse_is_not_a_round(self):
        marker = lambda n, v: f'✅ Code review **APPROVED**.\n<!-- bureau-review-round: round={n} verdict={v} -->'
        legacy = '🔄 Code Review: **Changes Requested** (cycle 0/3)'
        self.assertEqual(self.counts([legacy, marker(2, 'APPROVE'), marker(2, 'APPROVE')]), dict(cycles=1, rounds=2))
        self.assertEqual(self.counts([legacy, marker(2, 'APPROVE'), marker(2, 'REQUEST_CHANGES'), marker(3, 'BLOCK')]),
                         dict(cycles=2, rounds=3))
        self.assertEqual(self.counts([marker(7, 'REQUEST_CHANGES')]), dict(cycles=1, rounds=7))
        self.assertEqual(self.counts(['Code review round 3 started.\n<!-- bureau-review-round: round=3 -->', marker(3, 'APPROVE')]), dict(cycles=0, rounds=3))
        # Quoted previous markers in review prose cannot recycle the terminal
        # marker's round number or count that quotation as another rework cycle.
        self.assertEqual(self.counts([marker(1, 'REQUEST_CHANGES') + '\nQuoted old marker above.\n' + marker(3, 'APPROVE')]),
                         dict(cycles=0, rounds=3))
        with self.assertRaises(AssertionError):
            self.counts(['Code review round 3 started.\n<!-- bureau-review-round: round=oops -->'])

    def test_marker_mentions_and_quotations_are_not_bureau_metadata(self):
        marker = '<!-- bureau-review-round: round=99 verdict=REQUEST_CHANGES -->'
        examples = [
            'Please preserve <!-- bureau-review-round: round=N --> in the scripts.',
            marker + '\nFixed; please retry.', marker,
            '> ✅ Code review **APPROVED**.\n> ' + marker + '\nFixed; please retry.',
            '```\nCode review round 99 started.\n' + marker + '\n```',
            'Code review round 1 started.\nPlease preserve <!-- bureau-review-round: round=N -->.',
            'Code review round 1 started.\n> ' + marker,
            'Code review round 1 started.\n```html\n' + marker + '\n```',
            'Code review round 1 started.\n~~~html\n' + marker + '\n~~~',
            'Code review round 1 started.\n````\n```\n' + marker,
            'Code review round 1 started.\n    ' + marker,
        ]
        valid = 'Code review round 2 started.\n<!-- bureau-review-round: round=2 -->'
        legacy = '🔄 Code Review: **Changes Requested** (cycle 0/3)'
        for body in examples:
            with self.subTest(body=body):
                self.assertEqual(self.counts([legacy, body, valid]), dict(cycles=1, rounds=2))

    def test_all_bureau_review_headers_keep_the_terminal_round(self):
        headers = ['✅ Code review **APPROVED**. Stopped before merge as requested.',
                   '⏳ Code review **APPROVED**. PR #1 is not merged yet.',
                   '🛑 Code review **APPROVED**, but the merge gate is blocked.',
                   '🚫 Code review **BLOCKED** — needs human review.',
                   '🔄 Code Review: **Changes Requested** (cycle 0/3)',
                   '## Code Review — Changes Requested']
        marker = '<!-- bureau-review-round: round=3 verdict=REQUEST_CHANGES -->'
        for header in headers:
            with self.subTest(header=header):
                self.assertEqual(self.counts([header + '\n```html\n' + marker + '\n```\n' + marker]),
                                 dict(cycles=1, rounds=3))

    def test_inline_marker_examples_after_bureau_headers_do_not_halt_reviews(self):
        for suffix in (' in the scripts.', ' — quoted example', ' `example`'):
            body = 'Code review round 1 started.\n<!-- bureau-review-round: round=N -->' + suffix
            with self.subTest(suffix=suffix):
                self.assertEqual(self.counts([body]), dict(cycles=0, rounds=0))


class ApprovalPolicyTests(unittest.TestCase):
    def test_policy_matches_for_stops_and_gate_waits_and_legacy_fails_closed(self):
        spec = importlib.util.spec_from_file_location('policy_supervision', SCRIPTS / 'bureau-supervision.py')
        supervision = importlib.util.module_from_spec(spec); spec.loader.exec_module(supervision)
        detail = dict(identifier='T-1', title='Task', description='Required work', labels=[])
        policy = 'v1:BUG:3:4'
        with tempfile.TemporaryDirectory(prefix='bureau-pr8-policy-') as scratch:
            root = Path(scratch)
            record = dict(verdict='APPROVE', branch='001-task', state='Build Review', head='a'*40,
                          base='b'*40, base_ref='main', pr=1, ticket_hash=supervision.fingerprint(detail, 'T-1'),
                          review_policy=policy, stopped_at=100)
            for gate_wait in (False, True):
                for saved, current, expected in ((policy, policy, True), (policy, 'v1:MINOR:3:4', False),
                        (policy, 'v1:BUG:0:4', False), (policy, 'v1:BUG:3:1', False),
                        (policy, 'v2:BUG:3:4', False), (None, policy, False), (None, None, False)):
                    with self.subTest(gate_wait=gate_wait, saved=saved, current=current):
                        value = {**record, 'merge_gate_wait':gate_wait}
                        if saved is None: value.pop('review_policy')
                        else: value['review_policy'] = saved
                        supervision.save(root / 'review-stops.json', {'T-1':value})
                        reuse = supervision.reuse(root, 'T-1', '001-task', 'Build Review', 'a'*40, 'b'*40,
                                                  'main', 1, json.dumps(detail), current)
                        self.assertEqual(reuse['reuse'], expected)
                        self.assertNotIn('T-1', supervision.read(root / 'review-stops.json'))


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-pr8-usage-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('usage_evidence', SCRIPTS / 'bureau-evidence.py')
        self.e = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.e)
        self.store = self.e.Store(self.root / 'bureau', clock=lambda: 100)
        self.run = 'a' * 32; self.head = 'b' * 40
        self.store.begin_run(self.run, 'T-1', self.root, self.head)
        self.attempt = self.store.begin_attempt(self.run, 'T-1', 'code_review', 'stage', self.root, self.head)
        self.calls = 0

    def receipt(self, phase, usage=None, cost=.25, stage_attempt=None):
        self.calls += 1
        receipt = dict(run_id=self.run, issue='T-1', provider_attempt_id=f'{self.calls:032x}',
                       stage_attempt_id=stage_attempt or self.attempt, stage='code_review', phase=phase,
                       provider='claude', provider_exit=0, normalized_exit=0, outcome='complete',
                       start_head=self.head, end_head=self.head, started_at=100, ended_at=100,
                       provider_logs=str(self.root), usage=usage, total_cost_usd=cost)
        return self.store.put_receipt(receipt)

    def test_four_calls_numeric_usage_retries_deduplication_and_identity(self):
        paths = [self.receipt(phase, dict(input_tokens=i, output_tokens=i*2, cache_read_input_tokens=0))
                 for i, phase in enumerate(('correctness', 'security', 'performance', 'verdict'), 1)]
        foreign = self.store.begin_attempt(self.run, 'T-1', 'code_review', 'stage', self.root, self.head)
        foreign_path = self.receipt('verdict', dict(input_tokens=9999), stage_attempt=foreign)
        # Duplicate links and a foreign stage's otherwise valid receipt cannot inflate usage.
        manifest = self.store._manifest(self.run)
        attempt = next(a for a in manifest['attempts'] if a['attempt_id'] == self.attempt)
        attempt['receipts'] += [str(paths[0].relative_to(self.store.root)), str(foreign_path.relative_to(self.store.root))]
        self.store._save_manifest(manifest)
        record = self.store.review_round(self.run, self.attempt, 1, 'BUG', 'REQUEST_CHANGES', 'APPROVE')
        self.assertEqual(record['calls'], 4)
        self.assertEqual(record['usage'], dict(input_tokens=10, output_tokens=20, cached_input_tokens=0,
                                             cache_creation_input_tokens=None, total_cost_usd=1.0))
        self.assertEqual(self.store.review_round(self.run, self.attempt, 1, 'BUG', 'REQUEST_CHANGES', 'APPROVE'), record)
        self.receipt('security', dict(input_tokens=5, output_tokens=6, cached_input_tokens=0), cost=None)
        record = self.store.review_round(self.run, self.attempt, 1, 'BUG', 'REQUEST_CHANGES', 'APPROVE')
        self.assertEqual(record['calls'], 5); self.assertEqual(record['usage']['input_tokens'], 15)
        self.assertIsNone(record['usage']['total_cost_usd'])
        with self.assertRaises(ValueError): self.store.review_round(self.run, self.attempt, 2, 'BUG')

    def test_unknown_invalid_and_partial_measurements_stay_unknown(self):
        self.receipt('correctness', dict(input_tokens=0, output_tokens=0), cost=0)
        partial = self.store.review_round(self.run, self.attempt, 1, 'BUG')
        self.assertTrue(all(v is None for v in partial['usage'].values()))
        for phase in ('security', 'performance', 'verdict'):
            self.receipt(phase, dict(input_tokens='unknown', output_tokens=True), cost=float('nan'))
        record = self.store.review_round(self.run, self.attempt, 1, 'BUG')
        self.assertTrue(all(v is None for v in record['usage'].values()))
        self.assertNotIn('unknown', json.dumps(record))

    def test_known_metrics_sum_independently_and_preserve_zero(self):
        for phase in ('correctness', 'security', 'performance', 'verdict'):
            self.receipt(phase, dict(input_tokens=1, output_tokens=None if phase == 'security' else 2,
                                     cache_read_input_tokens=0, private_diagnostic='must not persist'), cost=0)
        record = self.store.review_round(self.run, self.attempt, 1, 'BUG')
        self.assertEqual(record['usage'], dict(input_tokens=4, output_tokens=None, cached_input_tokens=0,
                                             cache_creation_input_tokens=None, total_cost_usd=0))
        for path in self.store.manifest_path(self.run).parent.glob('receipts/*.json'):
            self.assertNotIn('must not persist', path.read_text())


class ReviewRoundPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-pr8-source-'); self.addCleanup(self.temp.cleanup)
        source = Path(self.temp.name).resolve()
        shutil.copytree(SCRIPTS, source / 'templates/scripts')
        shutil.copy(REVIEW, source / 'templates/scripts/code-review-pipeline.sh')
        shutil.copy(CONFIG, source / 'templates/scripts/bureau-config.sh')
        codex = fixture.CODEX.replace(
            "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))",
            """if 'Merge these three specialist reviews' in prompt and os.environ.get('FAIL_MERGER') == '1':
    sys.exit(1)
if 'Merge these three specialist reviews' in prompt:
    result = json.loads(os.environ['REVIEW_RESULT_JSON'])
elif 'specialist reviewing' in prompt:
    role = os.environ['BUREAU_PROVIDER_PHASE']; original = json.loads(os.environ['REVIEW_RESULT_JSON'])
    findings = original['findings'] if role == 'correctness' else []
    counts = {s.lower():sum(f['class'] == s for f in findings) for s in ('CRITICAL','BUG','MINOR','SKIP')}
    if role == 'security': counts['critical'] = int(os.environ.get('SECURITY_CRITICAL', '0'))
    gaps = original['missing_acceptance'] + [fix for fix in original['fixes_needed'] if fix not in [f['msg'] for f in original['findings']]]
    result = dict(specialist=role, counts=counts, findings=findings, summary=original['summary'],
                  acceptance_gaps=gaps if role == 'correctness' else [],
                  holds=['Fixture requested human judgment.'] if original['verdict'] == 'BLOCK' else [])
    if os.environ.get('FAIL_MERGER') == '1' and role == 'performance': result = 'Malformed fixture forces merger failure.'
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))""")
        codex = codex.replace("'usage':{'input_tokens':1,'output_tokens':1}", "'usage':json.loads(os.environ['ROUND_USAGE']).get(os.environ.get('BUREAU_PROVIDER_PHASE'), {})")
        self.fixture = fixture.SupervisionPipelineTests('test_fixture_commits_and_pushes_start_no_git_maintenance')
        with patch.object(fixture, 'ROOT', source), patch.object(fixture, 'CODEX', codex): self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo; self.root = self.fixture.root
        self.config = json.loads((self.repo / '.bureau.json').read_text())
        self.config['agents']['merge'] = True
        self.usage = {phase:dict(input_tokens=i, output_tokens=i*2, cached_input_tokens=0)
                      for i, phase in enumerate(('correctness', 'security', 'performance', 'verdict'), 1)}
        self.workers = 0

    def review(self, value, code=20, **env):
        (self.repo / '.bureau.json').write_text(json.dumps(self.config))
        self.workers += 1
        completed = subprocess.run(['bash', 'scripts/bureau-worker.sh', 'T-1', 'code-review-pipeline.sh',
                                    str(self.root / f'worker-{self.workers}'), '001-task'], cwd=self.repo,
                                   env={**self.fixture.env, 'BUREAU_NO_MERGE':'1', 'BUREAU_STOP_REQUESTED':'1',
                                        'REVIEW_RESULT_JSON':json.dumps(value), 'ROUND_USAGE':json.dumps(self.usage), **env},
                                   capture_output=True, text=True, timeout=45)
        self.assertEqual(completed.returncode, code, completed.stdout + completed.stderr)
        return completed

    def rounds(self):
        manifests = [json.loads(p.read_text()) for p in (self.repo / '.git/bureau/evidence/v1/runs').glob('*/manifest.json')]
        return [a['review_round'] for m in manifests for a in m['attempts'] if 'review_round' in a]

    def return_to_review(self):
        items = json.loads(self.fixture.linear.read_text())
        items['T-1']['state'] = dict(id='build_review', name='Build Review')
        self.fixture.linear.write_text(json.dumps(items))

    def test_minor_only_request_changes_converges(self):
        value = result(findings=[finding('MINOR'), finding('SKIP', 'A separate pinned decision')], insights=['Private observation canary.'])
        self.review(value)
        calls = [json.loads(line) for line in self.fixture.gh_log.read_text().splitlines()]
        body = next(args[args.index('--body')+1] for args in calls if args[:2] == ['pr', 'comment'] and '## Code Review v2' in args[-1])
        self.assertIn('**Verdict**: APPROVE', body)
        self.assertIn('"verdict":"APPROVE"', body)
        self.assertIn('Deferred below threshold (BUG)', body); self.assertIn('SKIP findings', body)
        self.assertNotIn('Private observation canary.', body)
        records = self.rounds(); self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record['number'], record['provider_verdict'], record['final_verdict']), (1, 'APPROVE', 'APPROVE'))
        self.assertEqual(record['calls'], 3); self.assertEqual(len(set(record['provider_attempt_ids'])), 3)
        self.assertEqual(record['usage']['input_tokens'], 6); self.assertEqual(record['usage']['output_tokens'], 12)
        self.assertEqual(record['usage']['cached_input_tokens'], 0)
        self.assertIsNone(record['usage']['total_cost_usd'])
        receipts = [json.loads(p.read_text()) for p in (self.repo / '.git/bureau/evidence/v1/runs').glob('*/receipts/*.json')]
        self.assertEqual({r['phase'] for r in receipts}, {'correctness', 'security', 'performance'})
        self.assertRegex(record['merge']['result_sha256'], r'^[a-f0-9]{64}$')

    def test_exact_total_boundary_and_legacy_cycle_cap(self):
        self.config['agents']['max_review_rounds'] = 2
        self.review(result(findings=[finding('BUG')]), code=0)
        self.return_to_review()
        self.review(result(findings=[finding('BUG')]), code=25)
        self.assertEqual(sorted(r['number'] for r in self.rounds()), [1, 2])
        self.assertEqual(next(r['final_verdict'] for r in self.rounds() if r['number'] == 2), 'BLOCK')

    def test_reused_approval_adds_neither_round_nor_tokens(self):
        self.config['agents']['max_review_rounds'] = 1
        self.review(result())  # Genuine convergence may approve even at N.
        before = self.rounds(); calls = len(self.fixture.models())
        self.review(result(), code=0, BUREAU_NO_MERGE='0', BUREAU_STOP_REQUESTED='0')
        self.assertEqual(self.rounds(), before)
        self.assertEqual(len(self.fixture.models()), calls)
        comments = json.loads(self.fixture.linear.read_text())['T-1']['comments']['nodes']
        self.assertEqual(sum('started.\n<!-- bureau-review-round:' in c['body'] for c in comments), 1)

    def test_stricter_severity_does_not_reuse_a_deferred_minor_approval(self):
        self.review(result())
        calls = len(self.fixture.models())
        self.config['agents']['review_min_severity'] = 'MINOR'
        self.review(result(), code=0, BUREAU_NO_MERGE='0', BUREAU_STOP_REQUESTED='0')
        self.assertEqual(len(self.fixture.models()), calls + 3)
        self.assertEqual(sorted(r['number'] for r in self.rounds()), [1, 2])
        self.assertEqual(next(r['final_verdict'] for r in self.rounds() if r['number'] == 2), 'REQUEST_CHANGES')

    def test_changed_round_limit_does_not_reuse_approval(self):
        self.review(result())
        self.config['agents']['max_review_rounds'] = 1
        self.review(result(findings=[finding('BUG')]), code=25, BUREAU_NO_MERGE='0', BUREAU_STOP_REQUESTED='0')
        self.assertEqual(sorted(r['number'] for r in self.rounds()), [1, 2])

    def test_changed_cycle_limit_does_not_reuse_approval(self):
        self.review(result())
        self.config['agents']['max_review_cycles'] = 0
        self.review(result(findings=[finding('BUG')]), code=25, BUREAU_NO_MERGE='0', BUREAU_STOP_REQUESTED='0')
        self.assertEqual(sorted(r['number'] for r in self.rounds()), [1, 2])

    def test_unmatched_required_fix_stays_in_rework(self):
        self.review(result(fixes_needed=['CRITICAL: save the replacement before deleting the original']), code=0)
        self.assertEqual(self.rounds()[0]['final_verdict'], 'REQUEST_CHANGES')

    def test_ordinary_marker_mention_does_not_stop_a_paid_review(self):
        items = json.loads(self.fixture.linear.read_text())
        items['T-1']['comments']['nodes'].append(dict(
            body='Please preserve <!-- bureau-review-round: round=N --> in the scripts.',
            createdAt='2026-09-09T01:00:00Z'))
        self.fixture.linear.write_text(json.dumps(items))
        self.review(result())
        self.assertEqual(self.rounds()[0]['number'], 1)

    def test_malformed_bureau_metadata_stops_before_provider_calls(self):
        items = json.loads(self.fixture.linear.read_text())
        items['T-1']['comments']['nodes'].append(dict(
            body='Code review round 1 started.\n<!-- bureau-review-round: round=oops -->',
            createdAt='2026-09-09T01:00:00Z'))
        self.fixture.linear.write_text(json.dumps(items))
        self.review(result(), code=27)
        self.assertEqual(self.fixture.models(), [])
        self.assertEqual(self.rounds(), [])

    def test_unknown_usage_and_stricter_minor_floor(self):
        self.config['agents']['review_min_severity'] = 'MINOR'
        self.usage['security'] = {}
        self.review(result(), code=0)
        record = self.rounds()[0]
        self.assertEqual((record['min_severity'], record['final_verdict']), ('MINOR', 'REQUEST_CHANGES'))
        self.assertTrue(all(value is None for value in record['usage'].values()))

    def test_critical_block_survives_red_build(self):
        self.config['repo']['test_command'] = 'exit 1'
        self.review(result('BLOCK', [finding('CRITICAL')]), code=25)
        self.assertEqual(self.rounds()[0]['final_verdict'], 'BLOCK')

    def test_review_block_does_not_invoke_sandbox_recovery(self):
        self.config['repo']['test_command'] = 'true'
        value = result('BLOCK', summary='SANDBOX_GATE: Operation not permitted; independent human hold')
        self.review(value, code=25)
        self.assertEqual(self.rounds()[0]['final_verdict'], 'BLOCK')
        manifest = next((self.repo / '.git/bureau/evidence/v1/runs').glob('*/manifest.json'))
        self.assertTrue(all(not a.get('gate_reruns') for a in json.loads(manifest.read_text())['attempts']))
        receipts = [json.loads(p.read_text()) for p in manifest.parent.glob('receipts/*.json')]
        self.assertTrue(all(not r.get('gate_attempts') for r in receipts))

    def test_failed_round_collects_available_call_identities(self):
        self.config['agents']['max_review_rounds'] = 2
        self.review(result(), code=22, FAIL_MERGER='1')
        record = self.rounds()[0]
        self.assertEqual(record['calls'], 4)
        self.assertIsNone(record['provider_verdict']); self.assertIsNone(record['final_verdict'])
        self.assertIsNone(record['usage']['input_tokens'])
        # The reserved round survives the missing final verdict/comment.
        self.review(result(findings=[finding('BUG')]), code=25)
        self.assertEqual(sorted(r['number'] for r in self.rounds()), [1, 2])


if __name__ == '__main__': unittest.main()
